"""Exports -- the store is the source of truth; these are views of it.

  markdown/<channel>/<date>--<title>--<id>.md
      YAML front-matter (ids, channel, duration, views, lane, interest,
      tags) + your note + the transcript as ~60 s paragraphs, each led by
      a [mm:ss](deep link). Drops straight into Obsidian, or into an LLM
      context window as a citable source.
  chunks.jsonl     one transcript chunk per line with denormalised
                   metadata -- ready for an embedding job / vector index
  videos.jsonl     one row per video (metadata + curation + transcript status)
  *.parquet        same two tables, if pyarrow is installed (DuckDB/Athena)
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from kb.urls import watch_url


def ts(seconds: float | None) -> str:
    if seconds is None:
        return ""
    s = int(seconds)
    h, m, s = s // 3600, s % 3600 // 60, s % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def slug(text: str | None, n: int = 60) -> str:
    s = re.sub(r"[^\w\s-]", "", (text or "untitled"), flags=re.UNICODE).strip().lower()
    return re.sub(r"[\s_-]+", "-", s)[:n].strip("-") or "untitled"


_VIDEO_SQL = """
SELECT v.video_id, v.title, v.channel_id, ch.title AS channel_title, ch.handle AS channel_handle,
       v.published_at, v.duration_s, v.view_count, v.like_count, v.comment_count, v.kind, v.tags,
       v.description, c.lane, c.interest, c.note, c.sources, c.tags AS board_tags,
       t.status AS transcript_status, t.language, t.source AS transcript_source, t.word_count
FROM videos v LEFT JOIN channels ch USING (channel_id) LEFT JOIN curation c USING (video_id)
LEFT JOIN transcripts t USING (video_id)
"""


def _yaml_val(v):
    if v is None:
        return "null"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, list):
        return "[" + ", ".join(json.dumps(x, ensure_ascii=False) for x in v) + "]"
    return json.dumps(str(v), ensure_ascii=False)


def markdown_for(conn, video_id: str) -> str | None:
    v = conn.execute(_VIDEO_SQL + " WHERE v.video_id=?", (video_id,)).fetchone()
    if not v:
        return None
    v = dict(v)
    tags = json.loads(v["tags"] or "[]")
    fm = {
        "video_id": v["video_id"], "title": v["title"], "url": watch_url(v["video_id"]),
        "channel": v["channel_title"], "channel_id": v["channel_id"], "handle": v["channel_handle"],
        "published": (v["published_at"] or "")[:10] or None, "duration": ts(v["duration_s"]),
        "duration_s": v["duration_s"], "views": v["view_count"], "kind": v["kind"],
        "lane": v["lane"], "interest": v["interest"], "transcript": v["transcript_status"],
        "language": v["language"], "transcript_source": v["transcript_source"], "words": v["word_count"],
        "tags": tags[:15],
    }
    lines = ["---", *(f"{k}: {_yaml_val(x)}" for k, x in fm.items()), "---", "",
             f"# {v['title'] or v['video_id']}", "",
             f"**{v['channel_title'] or v['channel_id'] or ''}** · {ts(v['duration_s'])} · "
             f"{v['view_count'] or 0:,} views · [watch]({watch_url(v['video_id'])})", ""]
    if v["note"]:
        lines += ["## Notes", "", v["note"], ""]
    chunks = conn.execute("SELECT start, text FROM chunks WHERE video_id=? ORDER BY idx", (video_id,)).fetchall()
    if chunks:
        lines += ["## Transcript", ""]
        for c in chunks:
            lines += [f"[{ts(c['start'])}]({watch_url(video_id, c['start'])}) {c['text']}", ""]
    elif v["transcript_status"]:
        lines += [f"_Transcript: {v['transcript_status']}_", ""]
    return "\n".join(lines)


def export_all(conn, out_dir: Path, markdown: bool = True, jsonl: bool = True, parquet: bool = True,
               only_transcribed: bool = True) -> dict:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    report = {"dir": str(out_dir)}
    where = " WHERE t.status='ok'" if only_transcribed else ""
    videos = [dict(r) for r in conn.execute(_VIDEO_SQL + where + " ORDER BY ch.title, v.published_at")]
    for v in videos:
        for k in ("tags", "sources", "board_tags"):
            v[k] = json.loads(v[k] or "[]")

    if markdown:
        n = 0
        for v in videos:
            md = markdown_for(conn, v["video_id"])
            d = out_dir / "markdown" / slug(v["channel_title"] or v["channel_id"] or "unknown", 40)
            d.mkdir(parents=True, exist_ok=True)
            name = f"{(v['published_at'] or '0000-00-00')[:10]}--{slug(v['title'])}--{v['video_id']}.md"
            (d / name).write_text(md, encoding="utf-8")
            n += 1
        report["markdown_files"] = n

    chunk_rows = [dict(r) for r in conn.execute("""
        SELECT c.video_id || ':' || c.idx AS id, c.video_id, c.idx, c.start, c.end, c.text,
               v.title, v.channel_id, ch.title AS channel_title, v.published_at, v.duration_s,
               v.view_count, t.language, t.source AS transcript_source
        FROM chunks c JOIN videos v USING (video_id) LEFT JOIN channels ch USING (channel_id)
        JOIN transcripts t USING (video_id) ORDER BY c.video_id, c.idx""")]
    for r in chunk_rows:
        r["url"] = watch_url(r["video_id"], r["start"])

    if jsonl:
        with (out_dir / "chunks.jsonl").open("w", encoding="utf-8") as f:
            for r in chunk_rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        with (out_dir / "videos.jsonl").open("w", encoding="utf-8") as f:
            for v in videos:
                f.write(json.dumps(v, ensure_ascii=False) + "\n")
        report["chunks_jsonl"], report["videos_jsonl"] = len(chunk_rows), len(videos)

    if parquet:
        try:
            import pyarrow as pa
            import pyarrow.parquet as pq
            pq.write_table(pa.Table.from_pylist(chunk_rows), out_dir / "chunks.parquet")
            pq.write_table(pa.Table.from_pylist(
                [{**v, "tags": json.dumps(v["tags"]), "sources": json.dumps(v["sources"]),
                  "board_tags": json.dumps(v["board_tags"])} for v in videos]), out_dir / "videos.parquet")
            report["parquet"] = True
        except ImportError:
            report["parquet"] = "skipped (pip install pyarrow)"
    return report
