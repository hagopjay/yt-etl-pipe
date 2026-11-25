"""All reads and writes against the SQLite store. Pure functions taking a
connection, so the CLI, HTTP server and worker share one code path."""

from __future__ import annotations

import json
import re
import sqlite3
import statistics
from datetime import datetime, timezone

from kb import transcripts as T
from kb.db import tx

LANES = ("inbox", "must_watch", "watching", "done", "skip")
_RANK_STEP = 1024.0


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _rows(cur) -> list[dict]:
    return [dict(r) for r in cur.fetchall()]


# ------------------------------------------------------------------ upserts
_CH_COLS = ("handle", "title", "description", "subscriber_count", "video_count", "view_count",
            "uploads_playlist_id", "thumbnail_url", "meta_source")
_V_COLS = ("channel_id", "title", "description", "published_at", "published_approx", "duration_s",
           "view_count", "like_count", "comment_count", "kind", "tags", "category_id", "language",
           "has_captions", "thumbnail_url", "meta_source", "meta_fetched_at")


def upsert_channel(conn, ch: dict) -> None:
    """Insert or merge. NULLs in `ch` never overwrite known values, so a
    thin source (RSS, Takeout) can't erase what the API filled in."""
    if not ch.get("channel_id"):
        return
    cols = [c for c in _CH_COLS if c in ch]
    vals = [ch[c] for c in cols]
    sets = ", ".join(f"{c}=COALESCE(excluded.{c}, channels.{c})" for c in cols)
    conn.execute(
        f"INSERT INTO channels(channel_id, {', '.join(cols)}, updated_at) VALUES (?, {', '.join('?' * len(cols))}, ?) "
        f"ON CONFLICT(channel_id) DO UPDATE SET {sets}{',' if sets else ''} updated_at=excluded.updated_at",
        [ch["channel_id"], *vals, now()])


def upsert_video(conn, v: dict) -> None:
    v = dict(v)
    if isinstance(v.get("tags"), list):
        v["tags"] = json.dumps(v["tags"], ensure_ascii=False)
    v.setdefault("meta_fetched_at", now() if v.get("meta_source") in ("api", "yt-dlp") else None)
    if v.get("channel_id"):
        conn.execute("INSERT OR IGNORE INTO channels(channel_id, title, meta_source) VALUES (?, ?, ?)",
                     (v["channel_id"], v.get("channel_title"), v.get("meta_source")))
        if v.get("channel_title") or v.get("channel_handle"):
            conn.execute("UPDATE channels SET title=COALESCE(title, ?), handle=COALESCE(handle, ?) WHERE channel_id=?",
                         (v.get("channel_title"), v.get("channel_handle"), v["channel_id"]))
    cols = [c for c in _V_COLS if c in v]
    sets = ", ".join(f"{c}=COALESCE(excluded.{c}, videos.{c})" for c in cols)
    conn.execute(
        f"INSERT INTO videos(video_id, {', '.join(cols)}, updated_at) VALUES (?, {', '.join('?' * len(cols))}, ?) "
        f"ON CONFLICT(video_id) DO UPDATE SET {sets}{',' if sets else ''} updated_at=excluded.updated_at",
        [v["video_id"], *[v[c] for c in cols], now()])


def ensure_video(conn, video_id: str, **known) -> None:
    conn.execute("INSERT OR IGNORE INTO videos(video_id) VALUES (?)", (video_id,))
    if known:
        upsert_video(conn, {"video_id": video_id, **{k: v for k, v in known.items() if v is not None}})


# ------------------------------------------------------------------- board
def _lane_bounds(conn, lane):
    r = conn.execute("SELECT MIN(rank), MAX(rank) FROM curation WHERE lane=?", (lane,)).fetchone()
    return r[0], r[1]


def add_to_board(conn, video_id: str, lane: str = "inbox", priority: float = 0.0,
                 sources: list[str] | None = None, tags: list[str] | None = None,
                 top: bool = False) -> bool:
    """Returns True if newly added. Re-adding merges sources; it never
    moves a card you've already placed."""
    if lane not in LANES:
        raise ValueError(f"lane must be one of {LANES}")
    ensure_video(conn, video_id)
    row = conn.execute("SELECT sources FROM curation WHERE video_id=?", (video_id,)).fetchone()
    if row:
        merged = sorted(set(json.loads(row["sources"] or "[]")) | set(sources or []))
        conn.execute("UPDATE curation SET sources=?, priority=MAX(priority, ?), updated_at=? WHERE video_id=?",
                     (json.dumps(merged), priority, now(), video_id))
        return False
    lo, hi = _lane_bounds(conn, lane)
    rank = (lo - _RANK_STEP if lo is not None else 0.0) if top else (hi + _RANK_STEP if hi is not None else 0.0)
    conn.execute(
        "INSERT INTO curation(video_id, lane, rank, priority, sources, tags, updated_at) VALUES (?,?,?,?,?,?,?)",
        (video_id, lane, rank, priority, json.dumps(sorted(set(sources or []))),
         json.dumps(tags or []), now()))
    return True


def move(conn, video_id: str, lane: str, before: str | None = None, after: str | None = None) -> float:
    """Place `video_id` in `lane` between `after` (above it) and `before`
    (below it). Fractional ranks: one UPDATE per drag, no renumbering,
    until float precision runs out -- then the lane is renormalised."""
    if lane not in LANES:
        raise ValueError(f"lane must be one of {LANES}")

    def rank_of(v):
        if not v:
            return None
        r = conn.execute("SELECT rank FROM curation WHERE video_id=? AND lane=?", (v, lane)).fetchone()
        return r[0] if r else None

    with tx(conn):
        if not conn.execute("SELECT 1 FROM curation WHERE video_id=?", (video_id,)).fetchone():
            add_to_board(conn, video_id, lane)
        a, b = rank_of(after), rank_of(before)
        lo, hi = _lane_bounds(conn, lane)
        if a is not None and b is not None:
            r = (a + b) / 2
            if r in (a, b):                       # precision exhausted
                _renormalise(conn, lane)
                a, b = rank_of(after), rank_of(before)
                r = (a + b) / 2
        elif a is not None:
            r = a + _RANK_STEP
        elif b is not None:
            r = b - _RANK_STEP
        else:
            r = (hi + _RANK_STEP) if hi is not None else 0.0
        conn.execute("UPDATE curation SET lane=?, rank=?, updated_at=? WHERE video_id=?", (lane, r, now(), video_id))
    return r


def _renormalise(conn, lane):
    ids = [r[0] for r in conn.execute("SELECT video_id FROM curation WHERE lane=? ORDER BY rank", (lane,))]
    conn.executemany("UPDATE curation SET rank=? WHERE video_id=?", [(i * _RANK_STEP, v) for i, v in enumerate(ids)])


def update_card(conn, video_id: str, **fields) -> None:
    allowed = {k: v for k, v in fields.items() if k in ("interest", "note")}
    if "lane" in fields and fields["lane"]:
        move(conn, video_id, fields["lane"])
    if allowed:
        if not conn.execute("SELECT 1 FROM curation WHERE video_id=?", (video_id,)).fetchone():
            add_to_board(conn, video_id)
        sets = ", ".join(f"{k}=?" for k in allowed)
        conn.execute(f"UPDATE curation SET {sets}, updated_at=? WHERE video_id=?", [*allowed.values(), now(), video_id])


def remove_card(conn, video_id: str) -> None:
    conn.execute("DELETE FROM curation WHERE video_id=?", (video_id,))


_CARD_SQL = """
SELECT v.video_id, v.title, v.duration_s, v.view_count, v.published_at, v.kind, v.thumbnail_url,
       v.channel_id, ch.title AS channel_title, ch.handle AS channel_handle, ch.followed,
       c.lane, c.rank, c.priority, c.interest, c.note, c.sources, c.tags, c.added_at,
       t.status AS transcript_status, t.reason AS transcript_reason, t.language AS transcript_language,
       t.word_count, t.source AS transcript_source,
       (SELECT j.status FROM jobs j WHERE j.kind='transcript' AND j.target=v.video_id
          AND j.status IN ('queued','running') LIMIT 1) AS transcript_job
FROM curation c
JOIN videos v USING (video_id)
LEFT JOIN channels ch ON ch.channel_id = v.channel_id
LEFT JOIN transcripts t ON t.video_id = v.video_id
"""


def _card(r: dict) -> dict:
    r["sources"] = json.loads(r.get("sources") or "[]")
    r["tags"] = json.loads(r.get("tags") or "[]")
    return r


def board(conn) -> dict:
    rows = _rows(conn.execute(_CARD_SQL + " ORDER BY c.lane, c.rank"))
    out = {lane: [] for lane in LANES}
    for r in rows:
        out[r["lane"]].append(_card(r))
    return out


# -------------------------------------------------------------- transcripts
def save_transcript(conn, res: T.Result) -> None:
    ensure_video(conn, res.video_id)
    with tx(conn):
        conn.execute("DELETE FROM transcripts WHERE video_id=?", (res.video_id,))  # cascades segments/chunks
        segs = res.segments
        conn.execute(
            "INSERT INTO transcripts(video_id, status, reason, language, is_generated, source, segment_count,"
            " word_count, full_text, error, fetched_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (res.video_id, res.status, res.reason, res.language,
             None if res.is_generated is None else int(res.is_generated), res.source,
             len(segs), T.word_count(segs) if segs else None,
             " ".join(s["text"] for s in segs) if segs else None, res.error, now()))
        if segs:
            conn.executemany("INSERT INTO segments(video_id, idx, start, duration, text) VALUES (?,?,?,?,?)",
                             [(res.video_id, i, s["start"], s["duration"], s["text"]) for i, s in enumerate(segs)])
            conn.executemany("INSERT INTO chunks(video_id, idx, start, end, text) VALUES (?,?,?,?,?)",
                             [(res.video_id, c["idx"], c["start"], c["end"], c["text"]) for c in T.chunk(segs)])


def rechunk_all(conn, **kw) -> int:
    """Rebuild every chunk from stored segments (after tuning chunk sizes)."""
    vids = [r[0] for r in conn.execute("SELECT video_id FROM transcripts WHERE status='ok'")]
    for vid in vids:
        segs = _rows(conn.execute("SELECT start, duration, text FROM segments WHERE video_id=? ORDER BY idx", (vid,)))
        with tx(conn):
            conn.execute("DELETE FROM chunks WHERE video_id=?", (vid,))
            conn.executemany("INSERT INTO chunks(video_id, idx, start, end, text) VALUES (?,?,?,?,?)",
                             [(vid, c["idx"], c["start"], c["end"], c["text"]) for c in T.chunk(segs, **kw)])
    return len(vids)


def video_detail(conn, video_id: str) -> dict | None:
    v = conn.execute("""
        SELECT v.*, ch.title AS channel_title, ch.handle AS channel_handle, ch.followed,
               c.lane, c.interest, c.note, c.sources, c.tags AS board_tags, c.priority,
               t.status AS transcript_status, t.reason AS transcript_reason, t.language AS transcript_language,
               t.is_generated, t.source AS transcript_source, t.word_count, t.fetched_at AS transcript_fetched_at,
               t.error AS transcript_error
        FROM videos v LEFT JOIN channels ch USING (channel_id)
        LEFT JOIN curation c USING (video_id) LEFT JOIN transcripts t USING (video_id)
        WHERE v.video_id=?""", (video_id,)).fetchone()
    if not v:
        return None
    d = dict(v)
    for k in ("tags", "sources", "board_tags"):
        d[k] = json.loads(d.get(k) or "[]")
    d["chunks"] = _rows(conn.execute("SELECT idx, start, end, text FROM chunks WHERE video_id=? ORDER BY idx", (video_id,)))
    d["transcript_job"] = (conn.execute(
        "SELECT status FROM jobs WHERE kind='transcript' AND target=? AND status IN ('queued','running')",
        (video_id,)).fetchone() or [None])[0]
    return d


# ---------------------------------------------------------------- channels
def set_channel(conn, channel_id: str, **fields) -> None:
    allowed = {k: v for k, v in fields.items() if k in ("followed", "interest", "notes")}
    if allowed:
        sets = ", ".join(f"{k}=?" for k in allowed)
        conn.execute(f"UPDATE channels SET {sets}, updated_at=? WHERE channel_id=?", [*allowed.values(), now(), channel_id])


def find_channel(conn, ref: str) -> dict | None:
    r = conn.execute("SELECT * FROM channels WHERE channel_id=? OR lower(handle)=lower(?)", (ref, ref)).fetchone()
    return dict(r) if r else None


def channels(conn) -> list[dict]:
    return _rows(conn.execute("""
        SELECT ch.channel_id, ch.handle, ch.title, ch.subscriber_count, ch.video_count, ch.thumbnail_url,
               ch.followed, ch.interest, ch.last_scraped_at, ch.meta_source,
               COUNT(v.video_id) AS stored_videos,
               COALESCE(SUM(v.duration_s), 0) AS total_duration_s,
               SUM(CASE WHEN t.status='ok' THEN 1 ELSE 0 END) AS transcripts_ok,
               SUM(CASE WHEN t.status='ok' THEN v.duration_s ELSE 0 END) AS transcribed_duration_s,
               SUM(CASE WHEN c.video_id IS NOT NULL THEN 1 ELSE 0 END) AS on_board
        FROM channels ch
        LEFT JOIN videos v ON v.channel_id = ch.channel_id
        LEFT JOIN transcripts t ON t.video_id = v.video_id
        LEFT JOIN curation c ON c.video_id = v.video_id
        GROUP BY ch.channel_id
        HAVING ch.followed = 1 OR stored_videos > 0
        ORDER BY ch.followed DESC, transcripts_ok DESC, stored_videos DESC, ch.title"""))


SORTS = {  # name -> (key, reverse)
    "views": (lambda v: v["view_count"] or 0, True),
    "views_per_day": (lambda v: v["views_per_day"] or 0, True),
    "breakout": (lambda v: v["breakout"] or 0, True),
    "longest": (lambda v: v["duration_s"] or 0, True),
    "newest": (lambda v: v["published_at"] or "", True),
    "oldest": (lambda v: v["published_at"] or "9999", False),
}


def channel_videos(conn, channel_id: str, sort: str = "views", kinds: tuple[str, ...] | None = None,
                   min_duration: int | None = None, limit: int | None = None) -> dict:
    """The channel's catalogue with popularity analytics:

    views_per_day  views / days since publish -- normalises for age, so a
                   3-week-old video at 200k ranks above a 6-year-old at 800k
    breakout       views / median views of the channel's same-kind uploads
                   -- "how far above this channel's normal did it go", the
                   cleanest signal of a video that actually landed
    """
    ch = conn.execute("SELECT * FROM channels WHERE channel_id=?", (channel_id,)).fetchone()
    if not ch:
        return {}
    vids = _rows(conn.execute("""
        SELECT v.video_id, v.title, v.duration_s, v.view_count, v.like_count, v.comment_count, v.published_at,
               v.published_approx, v.kind, v.thumbnail_url,
               t.status AS transcript_status, t.word_count, c.lane,
               (SELECT j.status FROM jobs j WHERE j.kind='transcript' AND j.target=v.video_id
                  AND j.status IN ('queued','running') LIMIT 1) AS transcript_job
        FROM videos v LEFT JOIN transcripts t USING (video_id) LEFT JOIN curation c USING (video_id)
        WHERE v.channel_id=?""", (channel_id,)))
    today = datetime.now(timezone.utc)
    medians = {}
    for kind in {v["kind"] for v in vids}:
        views = [v["view_count"] for v in vids if v["kind"] == kind and v["view_count"] is not None]
        medians[kind] = statistics.median(views) if views else None
    for v in vids:
        age = None
        if v["published_at"]:
            try:
                age = max((today - datetime.fromisoformat(v["published_at"].replace("Z", "+00:00"))).days, 1)
            except ValueError:
                pass
        v["age_days"] = age
        v["views_per_day"] = round(v["view_count"] / age, 1) if age and v["view_count"] is not None else None
        m = medians.get(v["kind"])
        v["breakout"] = round(v["view_count"] / m, 2) if m and v["view_count"] is not None else None
    total = len(vids)
    if kinds:
        vids = [v for v in vids if v["kind"] in kinds]
    if min_duration:
        vids = [v for v in vids if (v["duration_s"] or 0) >= min_duration]
    key, rev = SORTS.get(sort, SORTS["views"])
    vids.sort(key=key, reverse=rev)
    if limit:
        vids = vids[:limit]
    durs = [v["duration_s"] for v in vids if v["duration_s"]]
    return {
        "channel": dict(ch), "videos": vids, "total_stored": total,
        "summary": {
            "shown": len(vids), "total_duration_s": sum(durs),
            "median_duration_s": statistics.median(durs) if durs else None,
            "median_views": {k: m for k, m in medians.items()},
            "transcripts_ok": sum(1 for v in vids if v["transcript_status"] == "ok"),
            "kinds": {k: sum(1 for v in vids if v["kind"] == k) for k in {v["kind"] for v in vids}},
        },
    }


# ------------------------------------------------------------------ search
_MARK_OPEN, _MARK_CLOSE = "\x02", "\x03"   # escaped client-side; never raw HTML


def _fts_query(q: str) -> str:
    """Plain words -> AND of quoted terms (prefix on the last); keep the
    user's syntax if they wrote FTS5 operators/quotes themselves."""
    q = q.strip()
    if re.search(r'["*()]|\b(AND|OR|NOT|NEAR)\b', q):
        return q
    toks = [t for t in re.findall(r"[\w'-]+", q, re.UNICODE) if t]
    if not toks:
        return '""'
    parts = ['"' + t.replace('"', '""') + '"' for t in toks]
    parts[-1] += "*"
    return " ".join(parts)


def search(conn, q: str, channel_id: str | None = None, limit: int = 60) -> dict:
    fq = _fts_query(q)
    where_ch = "AND v.channel_id = :ch" if channel_id else ""
    params = {"q": fq, "ch": channel_id, "lim": limit}
    try:
        hits = _rows(conn.execute(f"""
            SELECT c.video_id, c.idx, c.start, c.end,
                   snippet(chunks_fts, 0, '{_MARK_OPEN}', '{_MARK_CLOSE}', ' … ', 24) AS snippet,
                   bm25(chunks_fts) AS score,
                   v.title, v.duration_s, v.view_count, v.channel_id, ch.title AS channel_title, cu.lane
            FROM chunks_fts
            JOIN chunks c ON c.chunk_id = chunks_fts.rowid
            JOIN videos v ON v.video_id = c.video_id
            LEFT JOIN channels ch ON ch.channel_id = v.channel_id
            LEFT JOIN curation cu ON cu.video_id = v.video_id
            WHERE chunks_fts MATCH :q {where_ch}
            ORDER BY score LIMIT :lim""", params))
        titles = _rows(conn.execute(f"""
            SELECT v.video_id, snippet(videos_fts, 0, '{_MARK_OPEN}', '{_MARK_CLOSE}', ' … ', 16) AS title_hl,
                   v.title, v.duration_s, v.view_count, v.channel_id, ch.title AS channel_title,
                   t.status AS transcript_status, cu.lane, bm25(videos_fts, 10.0, 1.0) AS score
            FROM videos_fts JOIN videos v ON v.rowid = videos_fts.rowid
            LEFT JOIN channels ch ON ch.channel_id = v.channel_id
            LEFT JOIN transcripts t ON t.video_id = v.video_id
            LEFT JOIN curation cu ON cu.video_id = v.video_id
            WHERE videos_fts MATCH :q {where_ch}
            ORDER BY score LIMIT 25""", params))
    except sqlite3.OperationalError as e:
        return {"query": q, "fts": fq, "error": str(e), "videos": [], "titles": []}
    grouped: dict[str, dict] = {}
    for h in hits:
        g = grouped.setdefault(h["video_id"], {k: h[k] for k in (
            "video_id", "title", "duration_s", "view_count", "channel_id", "channel_title", "lane")} | {"hits": [], "best": h["score"]})
        g["hits"].append({"start": h["start"], "end": h["end"], "snippet": h["snippet"], "score": h["score"]})
    for g in grouped.values():
        g["hits"].sort(key=lambda x: x["start"])
    return {"query": q, "fts": fq, "videos": sorted(grouped.values(), key=lambda g: g["best"]),
            "titles": titles, "marks": [_MARK_OPEN, _MARK_CLOSE]}


# ------------------------------------------------------------------- stats
def stats(conn) -> dict:
    one = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
    return {
        "videos": one("SELECT COUNT(*) FROM videos"),
        "channels_followed": one("SELECT COUNT(*) FROM channels WHERE followed=1"),
        "on_board": one("SELECT COUNT(*) FROM curation WHERE lane NOT IN ('done','skip')"),
        "must_watch": one("SELECT COUNT(*) FROM curation WHERE lane='must_watch'"),
        "transcripts_ok": one("SELECT COUNT(*) FROM transcripts WHERE status='ok'"),
        "transcript_words": one("SELECT COALESCE(SUM(word_count),0) FROM transcripts WHERE status='ok'"),
        "transcribed_hours": round(one("""SELECT COALESCE(SUM(v.duration_s),0) FROM transcripts t
                                          JOIN videos v USING(video_id) WHERE t.status='ok'""") / 3600, 1),
        "transcripts_by_status": dict(conn.execute("SELECT status, COUNT(*) FROM transcripts GROUP BY status").fetchall()),
    }
