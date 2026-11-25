"""Fetch a transcript and classify every outcome, then cut it into
searchable, timestamped chunks.

Outcome taxonomy (fixes the lambda/fetch_captions.py misrouting that
tests/test_findings_pipeline.py pins: AgeRestricted / InvalidVideoId /
VideoUnplayable are PERMANENT, not "blocked"):

  ok            got captions
  disabled      creator turned captions off          terminal, respected
  no_captions   captions exist in no language        terminal
  unavailable   private / deleted / age-restricted / members-only / bad id
                                                     terminal
  Blocked       IP or bot-check block               raise -> worker pauses globally
  Transient     anything else from the library      raise -> worker retries with backoff

Language choice: preferred languages first, human-made before auto, then
any human-made, then any auto. A human-made Spanish track beats an
auto-generated English translation for fidelity; the language is stored
either way so nothing is mislabeled.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field

from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api import _errors as E

_BLOCKED = tuple(getattr(E, n) for n in ("RequestBlocked", "IpBlocked") if hasattr(E, n))
_UNAVAILABLE = {
    "VideoUnavailable": "unavailable",
    "VideoUnplayable": "unplayable",
    "AgeRestricted": "age_restricted",
    "InvalidVideoId": "invalid_id",
}


class Blocked(Exception):
    """YouTube refused this IP. Retrying immediately makes it worse."""


class Transient(Exception):
    """Unclassified library failure -- retry later, give up after a few."""


@dataclass
class Result:
    status: str
    video_id: str
    language: str | None = None
    is_generated: bool | None = None
    segments: list[dict] = field(default_factory=list)
    reason: str | None = None
    error: str | None = None

    @property
    def source(self) -> str | None:
        if self.status != "ok":
            return None
        return "caption_auto" if self.is_generated else "caption_manual"


def make_client(settings=None) -> YouTubeTranscriptApi:
    if settings is not None and settings.proxy_user:
        from youtube_transcript_api.proxies import WebshareProxyConfig
        return YouTubeTranscriptApi(proxy_config=WebshareProxyConfig(
            proxy_username=settings.proxy_user, proxy_password=settings.proxy_pass,
            retries_when_blocked=3))
    if settings is not None and settings.proxy_url:
        from youtube_transcript_api.proxies import GenericProxyConfig
        return YouTubeTranscriptApi(proxy_config=GenericProxyConfig(
            http_url=settings.proxy_url, https_url=settings.proxy_url))
    return YouTubeTranscriptApi()


def _pick(tlist, langs):
    for finder in ("find_manually_created_transcript", "find_generated_transcript"):
        try:
            return getattr(tlist, finder)(list(langs))
        except E.NoTranscriptFound:
            pass
    tracks = list(tlist)
    if not tracks:
        raise E.NoTranscriptFound("", list(langs), tlist)
    tracks.sort(key=lambda t: t.is_generated)  # human-made first
    return tracks[0]


def fetch(video_id: str, client: YouTubeTranscriptApi | None = None,
          langs: tuple[str, ...] = ("en", "en-US", "en-GB")) -> Result:
    client = client or YouTubeTranscriptApi()
    try:
        track = _pick(client.list(video_id), langs)
        fetched = track.fetch()
    except E.TranscriptsDisabled:
        return Result("disabled", video_id, reason="disabled_by_creator")
    except E.NoTranscriptFound:
        return Result("no_captions", video_id)
    except _BLOCKED as e:
        raise Blocked(f"{type(e).__name__}: {_short(e)}") from e
    except E.CouldNotRetrieveTranscript as e:
        name = type(e).__name__
        if name in _UNAVAILABLE:
            return Result("unavailable", video_id, reason=_UNAVAILABLE[name], error=_short(e))
        raise Transient(f"{name}: {_short(e)}") from e

    segs = []
    for s in fetched.snippets:
        text = clean(s.text)
        if text:
            segs.append({"start": float(s.start), "duration": float(s.duration), "text": text})
    if not segs:
        return Result("no_captions", video_id, reason="empty_track")
    return Result("ok", video_id, language=fetched.language_code,
                  is_generated=bool(fetched.is_generated), segments=segs)


def _short(e: Exception) -> str:
    # The library's messages are multi-paragraph help text; keep the cause line.
    return str(e).strip().splitlines()[0][:300] if str(e).strip() else type(e).__name__


_WS = re.compile(r"\s+")


def clean(text: str) -> str:
    return _WS.sub(" ", html.unescape(text or "")).strip()


# ------------------------------------------------------------------ chunking
_SENT_END = re.compile(r"[.!?…。！？]['\")\]]*$")


def chunk(segments: list[dict], target_s: float = 60.0, min_s: float = 35.0,
          max_s: float = 95.0) -> list[dict]:
    """Group segments into ~target_s windows. Prefer to close a chunk at a
    sentence end once past min_s (manual captions have punctuation); hard
    close at max_s (auto-captions mostly don't). Never split a segment, so
    every chunk start is a real caption timestamp you can seek to."""
    chunks, cur = [], []

    def close():
        if cur:
            start = cur[0]["start"]
            end = cur[-1]["start"] + cur[-1]["duration"]
            chunks.append({"idx": len(chunks), "start": round(start, 2), "end": round(end, 2),
                           "text": " ".join(s["text"] for s in cur)})
            cur.clear()

    for s in segments:
        cur.append(s)
        span = s["start"] + s["duration"] - cur[0]["start"]
        if span >= max_s or (span >= min_s and _SENT_END.search(s["text"])) or \
                (span >= target_s and not any(_SENT_END.search(x["text"]) for x in cur)):
            close()
    close()
    return chunks


def word_count(segments: list[dict]) -> int:
    return sum(len(s["text"].split()) for s in segments)
