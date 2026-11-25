"""
ingest/queue_builder.py -- merge activity signals into one prioritized,
de-duplicated transcript queue, and carve out a stratified test sample
that exercises every branch of the pipeline.

Priority (higher = sooner), all explainable from the item itself:
  + 100  in Watch Later            explicit "I want this" intent
  +  15  per extra rewatch (cap 4) rewatching is strong interest
  +  40  new upload from a subscribed channel
  +  20  in any other saved playlist
  +  0..30 recency of the latest touch (30-day half-life)
  -  25  YouTube Music listen      music rarely carries useful transcripts
"""

from __future__ import annotations

import json
import math
import random
import re
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from ingest.sources import Signal

HALF_LIFE_DAYS = 30.0

# Title heuristics -> which pipeline branch a video is likely to hit.
# Cheap guesses from Takeout metadata only; videos.list (Data API) can
# replace them with real duration / caption / category facts later.
_TAGS = [
    ("music", re.compile(r"official (music )?video|\(official audio\)|\blyrics?\b|\bft\.|\bfeat\.|\bremix\b", re.I)),
    ("long_form", re.compile(r"\bpodcast\b|\bep(isode)?\.? ?\d+|\bfull (course|lecture|movie|album)\b|\blive ?stream\b|\b\d+ ?hours?\b|#\d{2,}", re.I)),
    ("live", re.compile(r"\blive\b|\bpremiere\b|\bstream(ed|ing)?\b", re.I)),
    ("shorts", re.compile(r"#shorts?\b", re.I)),
    ("non_latin", re.compile(r"[^\x00-ɏ -⁯☀-➿\U0001F000-\U0001FAFF\s]")),
]


@dataclass
class QueueItem:
    video_id: str
    url: str
    priority: float
    sources: list[str]
    watch_count: int = 0
    first_seen: str | None = None
    last_touched: str | None = None
    title: str | None = None
    channel: str | None = None
    channel_id: str | None = None
    tags: list[str] = field(default_factory=list)


def _tags(title: str | None, is_music_product: bool) -> list[str]:
    tags = [name for name, rx in _TAGS if title and rx.search(title)]
    if is_music_product and "music" not in tags:
        tags.append("music")
    return tags or ["general"]


def build_queue(signals: Iterable[Signal], now: datetime | None = None,
                include_music: bool = True, since: datetime | None = None) -> list[QueueItem]:
    now = now or datetime.now(timezone.utc)
    by_id: dict[str, list[Signal]] = defaultdict(list)
    for s in signals:
        if since and s.at and s.at < since:
            continue
        by_id[s.video_id].append(s)

    items: list[QueueItem] = []
    for vid, sigs in by_id.items():
        sources = sorted({s.source for s in sigs})
        watches = [s for s in sigs if s.source == "watch_history"]
        music = any(s.extra.get("product") == "YouTube Music" for s in watches)
        if music and not include_music and "watch_later" not in sources:
            continue
        stamps = [s.at for s in sigs if s.at]
        last = max(stamps) if stamps else None
        meta = next((s for s in sigs if s.title), sigs[0])

        score = 0.0
        if "watch_later" in sources:
            score += 100
        score += 15 * min(max(len(watches) - 1, 0), 4)
        if "subscription_feed" in sources:
            score += 40
        if any(src.startswith("playlist:") for src in sources):
            score += 20
        if last:
            age_days = max((now - last).total_seconds() / 86400, 0)
            score += 30 * math.pow(0.5, age_days / HALF_LIFE_DAYS)
        if music:
            score -= 25

        items.append(QueueItem(
            video_id=vid, url=f"https://www.youtube.com/watch?v={vid}", priority=round(score, 2),
            sources=sources, watch_count=len(watches),
            first_seen=min(stamps).isoformat() if stamps else None,
            last_touched=last.isoformat() if last else None,
            title=meta.title, channel=meta.channel, channel_id=meta.channel_id,
            tags=_tags(meta.title, music),
        ))
    items.sort(key=lambda i: (-i.priority, i.video_id))
    return items


def stratified_sample(items: list[QueueItem], per_stratum: int = 3, seed: int = 7) -> list[QueueItem]:
    """Pick a small set that touches every source and every heuristic tag,
    so one seeding run exercises captions-manual, captions-auto, no-
    captions/ASR, long-payload, music/disabled and non-English branches
    instead of 500 near-identical happy paths."""
    rng = random.Random(seed)
    strata: dict[str, list[QueueItem]] = defaultdict(list)
    for it in items:
        for src in it.sources:
            strata[f"source:{src.split(':')[0]}"].append(it)
        for t in it.tags:
            strata[f"tag:{t}"].append(it)
    chosen: dict[str, QueueItem] = {}
    for key in sorted(strata):
        pool = [i for i in strata[key] if i.video_id not in chosen]
        for it in rng.sample(pool, min(per_stratum, len(pool))):
            chosen[it.video_id] = it
    return sorted(chosen.values(), key=lambda i: (-i.priority, i.video_id))


def summarize(items: list[QueueItem]) -> dict:
    src, tag = defaultdict(int), defaultdict(int)
    for it in items:
        for s in it.sources:
            src[s.split(":")[0]] += 1
        for t in it.tags:
            tag[t] += 1
    return {"videos": len(items), "by_source": dict(src), "by_tag": dict(tag),
            "rewatched": sum(1 for i in items if i.watch_count > 1)}


def write_jsonl(items: list[QueueItem], path: Path) -> None:
    with Path(path).open("w", encoding="utf-8") as f:
        for it in items:
            f.write(json.dumps(asdict(it), ensure_ascii=False) + "\n")


def read_jsonl(path: Path) -> list[QueueItem]:
    return [QueueItem(**json.loads(line)) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]
