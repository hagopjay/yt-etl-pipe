"""Classify whatever gets pasted into the add box: a video, a channel, or
a playlist. Superset of lambda/youtube_url.py -- also handles /live/ID and
youtube-nocookie embeds (the two shapes the assessment tests flag as
unsupported there) plus every channel URL form."""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lambda"))
from youtube_url import extract_video_id as _base_extract  # noqa: E402

_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_UC = re.compile(r"^UC[A-Za-z0-9_-]{22}$")
_HANDLE = re.compile(r"^@[A-Za-z0-9._-]{3,100}$")
_EXTRA = [
    re.compile(r"youtube\.com/live/([A-Za-z0-9_-]{11})"),
    re.compile(r"youtube-nocookie\.com/embed/([A-Za-z0-9_-]{11})"),
]


@dataclass(frozen=True)
class Ref:
    kind: str     # video | channel | playlist
    value: str    # video_id | channel ref (UC id, @handle, or URL) | playlist id
    raw: str


def video_id(raw: str) -> str | None:
    raw = raw.strip()
    vid = _base_extract(raw)
    if vid:
        return vid
    for rx in _EXTRA:
        m = rx.search(raw)
        if m:
            return m.group(1)
    return None


def channel_ref(raw: str) -> str | None:
    """Returns 'UC…' or '@handle' or a canonical channel URL (for /c/ and
    /user/ forms, which need a resolver)."""
    raw = raw.strip()
    if _UC.match(raw) or _HANDLE.match(raw):
        return raw
    if "youtube.com" not in raw:
        return None
    u = urlparse(raw if "://" in raw else "https://" + raw)
    parts = [p for p in u.path.split("/") if p]
    if not parts:
        return None
    if parts[0] == "channel" and len(parts) > 1 and _UC.match(parts[1]):
        return parts[1]
    if parts[0].startswith("@"):
        return parts[0]
    if parts[0] in ("c", "user") and len(parts) > 1:
        return f"https://www.youtube.com/{parts[0]}/{parts[1]}"
    return None


def classify(raw: str) -> Ref | None:
    raw = raw.strip()
    if not raw or raw.startswith("#"):
        return None
    if "list=" in raw and "watch" not in raw:
        pl = parse_qs(urlparse(raw).query).get("list", [None])[0]
        if pl:
            return Ref("playlist", pl, raw)
    vid = video_id(raw)
    if vid and not _UC.match(raw):
        return Ref("video", vid, raw)
    ch = channel_ref(raw)
    if ch:
        return Ref("channel", ch, raw)
    return None


def parse_many(text: str) -> tuple[list[Ref], list[str]]:
    """Split pasted text on whitespace/commas; return (refs, unrecognised)."""
    refs, bad, seen = [], [], set()
    for tok in re.split(r"[\s,]+", text or ""):
        if not tok:
            continue
        r = classify(tok)
        if r is None:
            bad.append(tok)
        elif (r.kind, r.value) not in seen:
            seen.add((r.kind, r.value))
            refs.append(r)
    return refs, bad


def watch_url(vid: str, t: float | None = None) -> str:
    return f"https://www.youtube.com/watch?v={vid}" + (f"&t={int(t)}s" if t else "")
