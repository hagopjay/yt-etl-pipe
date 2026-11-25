"""
ingest/sources.py -- parsers that turn a person's own YouTube activity
into candidate videos for the transcript queue.

Why Takeout and not the Data API: since September 2016 the Data API
returns the placeholder IDs HL / WL for watch history and Watch Later and
no items for them, and the home feed was never exposed. Google Takeout
("YouTube and YouTube Music" -> history, playlists, subscriptions) is the
only sanctioned export of all three. The subscription feed can also be
rebuilt live from each subscribed channel's public RSS feed.

Every parser yields `Signal` records; queue_builder.py merges them.
Parsers are pure (path or text in, records out) so they test without AWS.
"""

from __future__ import annotations

import csv
import html
import io
import json
import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lambda"))
from youtube_url import extract_video_id  # noqa: E402  (shared with submit_handler)

_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")


@dataclass
class Signal:
    video_id: str
    source: str                      # watch_history | watch_later | playlist:<name> | subscription_feed | url_list
    at: datetime | None = None       # when watched / added / published
    title: str | None = None
    channel: str | None = None
    channel_id: str | None = None
    extra: dict = field(default_factory=dict)


def _parse_ts(raw: str | None) -> datetime | None:
    if not raw:
        return None
    raw = raw.strip()
    try:  # ISO 8601 (Takeout JSON, playlist CSV, RSS)
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    # Takeout HTML: "Nov 20, 2026, 9:14:03 PM EST" (zone abbreviation varies; drop it,
    # and the narrow no-break space newer exports put before AM/PM).
    cleaned = re.sub(r"\s+[A-Z]{2,5}$", "", raw.replace(" ", " "))
    for fmt in ("%b %d, %Y, %I:%M:%S %p", "%d %b %Y, %H:%M:%S"):
        try:
            return datetime.strptime(cleaned, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


# ------------------------------------------------------------ watch history
def parse_watch_history_json(path: Path) -> Iterator[Signal]:
    """Takeout `history/watch-history.json` (choose JSON in Takeout's
    format options). Skips ads ("From Google Ads"), removed/private
    videos (no titleUrl), and non-video activity (posts, searches)."""
    entries = json.loads(Path(path).read_text(encoding="utf-8"))
    for e in entries:
        details = {d.get("name") for d in e.get("details", [])}
        if "From Google Ads" in details:
            continue
        vid = extract_video_id(e.get("titleUrl") or "")
        if not vid:
            continue
        sub = (e.get("subtitles") or [{}])[0]
        title = e.get("title", "")
        title = re.sub(r"^Watched\s+", "", title)
        yield Signal(
            video_id=vid, source="watch_history", at=_parse_ts(e.get("time")),
            title=title, channel=sub.get("name"),
            channel_id=(sub.get("url") or "").rsplit("/", 1)[-1] or None,
            extra={"product": e.get("header", "YouTube")},  # "YouTube Music" entries are flagged
        )


_HTML_CELL = re.compile(
    r'<div class="content-cell[^"]*mdl-typography--body-1">(.*?)</div>', re.S)
_HTML_LINK = re.compile(r'<a href="([^"]+)">(.*?)</a>', re.S)


def parse_watch_history_html(path: Path) -> Iterator[Signal]:
    """Takeout's default `watch-history.html`. Each entry is a content-cell
    div: 'Watched <a video>title</a><br><a channel>name</a><br>timestamp'."""
    text = Path(path).read_text(encoding="utf-8")
    for cell in _HTML_CELL.findall(text):
        links = _HTML_LINK.findall(cell)
        if not links:
            continue
        vid = extract_video_id(html.unescape(links[0][0]))
        if not vid:
            continue
        parts = [p.strip() for p in re.split(r"<br\s*/?>", cell) if p.strip()]
        ts = html.unescape(re.sub(r"<[^>]+>", "", parts[-1])) if parts else None
        yield Signal(
            video_id=vid, source="watch_history", at=_parse_ts(ts),
            title=html.unescape(links[0][1]),
            channel=html.unescape(links[1][1]) if len(links) > 1 else None,
            channel_id=links[1][0].rsplit("/", 1)[-1] if len(links) > 1 else None,
        )


# ---------------------------------------------------------------- playlists
def parse_playlist_csv(path: Path, name: str | None = None) -> Iterator[Signal]:
    """Takeout `playlists/<Name>-videos.csv` -- including
    `Watch later-videos.csv`, the user's queue. Column headers are
    localized, so this keys off position (video id, added timestamp) and
    skips any row whose first cell isn't an 11-char ID (older exports had
    a metadata preamble)."""
    path = Path(path)
    name = name or path.stem.removesuffix("-videos")
    source = "watch_later" if name.lower() in ("watch later", "wl") else f"playlist:{name}"
    with path.open(encoding="utf-8-sig", newline="") as f:
        for row in csv.reader(f):
            if row and _ID.match(row[0].strip()):
                yield Signal(video_id=row[0].strip(), source=source,
                             at=_parse_ts(row[1]) if len(row) > 1 else None)


# ------------------------------------------------------ subscriptions / feed
def parse_subscriptions_csv(path: Path) -> list[dict]:
    """Takeout `subscriptions/subscriptions.csv`: Channel Id, Channel Url, Channel Title."""
    with Path(path).open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.reader(f))
    return [{"channel_id": r[0].strip(), "title": r[2].strip() if len(r) > 2 else None}
            for r in rows if r and r[0].strip().startswith("UC")]


_ATOM = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}


def parse_channel_rss(xml_text: str) -> Iterator[Signal]:
    """A channel's public feed (https://www.youtube.com/feeds/videos.xml?channel_id=UC...):
    the latest ~15 uploads. Stitching these together for every
    subscription rebuilds the 'Subscriptions' feed with no API key."""
    root = ET.fromstring(xml_text)
    channel = root.findtext("a:title", namespaces=_ATOM)
    for entry in root.findall("a:entry", _ATOM):
        vid = entry.findtext("yt:videoId", namespaces=_ATOM)
        if vid and _ID.match(vid):
            yield Signal(video_id=vid, source="subscription_feed",
                         at=_parse_ts(entry.findtext("a:published", namespaces=_ATOM)),
                         title=entry.findtext("a:title", namespaces=_ATOM), channel=channel,
                         channel_id=entry.findtext("yt:channelId", namespaces=_ATOM))


def rss_url(channel_id: str) -> str:
    return f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"


# ----------------------------------------------------------------- misc
def parse_url_list(text: str) -> Iterator[Signal]:
    """One URL or bare ID per line -- e.g. a hand-copied 'up next' queue."""
    for line in io.StringIO(text):
        vid = extract_video_id(line.strip()) if line.strip() and not line.startswith("#") else None
        if vid:
            yield Signal(video_id=vid, source="url_list")


def discover_takeout(root: Path) -> dict[str, list[Path]]:
    """Find the relevant files anywhere under an extracted Takeout folder
    (folder names are localized, file names mostly aren't)."""
    root = Path(root)
    found: dict[str, list[Path]] = {"history_json": [], "history_html": [], "playlists": [], "subscriptions": []}
    for p in root.rglob("*"):
        n = p.name.lower()
        if n == "watch-history.json":
            found["history_json"].append(p)
        elif n == "watch-history.html":
            found["history_html"].append(p)
        elif n.endswith("-videos.csv"):
            found["playlists"].append(p)
        elif n == "subscriptions.csv":
            found["subscriptions"].append(p)
    return found


def load_takeout(root: Path) -> tuple[list[Signal], list[dict]]:
    files = discover_takeout(root)
    signals: list[Signal] = []
    for p in files["history_json"]:
        signals.extend(parse_watch_history_json(p))
    if not files["history_json"]:  # JSON and HTML are alternative formats of the same data
        for p in files["history_html"]:
            signals.extend(parse_watch_history_html(p))
    for p in files["playlists"]:
        signals.extend(parse_playlist_csv(p))
    subs: list[dict] = []
    for p in files["subscriptions"]:
        subs.extend(parse_subscriptions_csv(p))
    return signals, subs


def iter_all(groups: Iterable[Iterable[Signal]]) -> Iterator[Signal]:
    for g in groups:
        yield from g
