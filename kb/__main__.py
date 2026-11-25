"""ytkb command line. Run `python -m kb <command> -h` for options.

  serve          UI + background worker            python -m kb serve --open
  add            videos/channels onto the board     python -m kb add URL... [--lane must_watch]
  follow         scrape a channel (+ transcripts)   python -m kb follow @ZacharyLLM --transcripts all
  work           drain the job queue in the foreground (no UI)
  top            a followed channel's catalogue, ranked
  search         full-text search over transcripts
  import-takeout Google Takeout -> board (via ingest/)
  import-queue   ingest/ queue.jsonl -> board
  export         markdown / jsonl / parquet
  status         counts, queue, pause state
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from kb import config, db, export, jobs, store


def _conn(s):
    return db.open_store(s.db_path)


def _fmt_dur(sec):
    return export.ts(sec) if sec else "-"


def cmd_serve(s, a):
    from kb.server import serve
    serve(s, host=a.host, port=a.port, worker=not a.no_worker, open_browser=a.open)


def cmd_add(s, a):
    c = _conn(s)
    text = " ".join(a.refs) + ("\n" + Path(a.file).read_text(encoding="utf-8") if a.file else "")
    follow = {"transcripts": a.channel_transcripts}
    out = jobs.intake(c, text, lane=a.lane, transcript=True if a.transcript else None, follow=follow)
    print(json.dumps(out, indent=2))
    if a.run:
        _work(s, c)


def cmd_follow(s, a):
    c = _conn(s)
    tabs = ["videos", "streams"] + (["shorts"] if a.shorts else [])
    for ref in a.refs:
        jobs.enqueue(c, "scrape_channel", ref, args={
            "transcripts": a.transcripts, "tabs": tabs, "min_duration": a.min_duration,
            "limit": a.limit, "board": a.board}, priority=jobs.PRIO_BOARD)
        print(f"queued scrape: {ref}  transcripts={a.transcripts} tabs={','.join(tabs)} min_duration={a.min_duration}s")
    if not a.no_run:
        _work(s, c)


def _work(s, c, max_jobs=None):
    w = jobs.Worker(s, c)
    print(f"working (metadata backend: {w.meta.backend}, delay {s.delay}s) -- Ctrl-C to stop; progress is saved")
    try:
        n = w.run(max_jobs=max_jobs, exit_when_idle=True)
    except KeyboardInterrupt:
        n = w.processed
    print(f"processed {n} jobs")
    print(json.dumps(jobs.summary(c)["counts"], indent=2))


def cmd_work(s, a):
    _work(s, _conn(s), max_jobs=a.max)


def cmd_top(s, a):
    c = _conn(s)
    ch = store.find_channel(c, a.channel)
    if not ch:
        sys.exit(f"unknown channel {a.channel} -- run `python -m kb follow {a.channel}` first")
    data = store.channel_videos(c, ch["channel_id"], sort=a.sort, kinds=tuple(a.kinds.split(",")),
                                min_duration=a.min_duration, limit=a.limit)
    print(f"{ch['title']}  ({ch.get('handle') or ch['channel_id']})  "
          f"{data['total_stored']} stored, showing {len(data['videos'])} by {a.sort}")
    print(f"{'#':>3} {'views':>11} {'v/day':>8} {'×med':>6} {'len':>8} {'date':>10}  T  title")
    for i, v in enumerate(data["videos"], 1):
        t = {"ok": "●", None: "·"}.get(v["transcript_status"], "×") if not v["transcript_job"] else "…"
        print(f"{i:>3} {v['view_count'] or 0:>11,} {v['views_per_day'] or 0:>8,.0f} {v['breakout'] or 0:>6.1f} "
              f"{_fmt_dur(v['duration_s']):>8} {(v['published_at'] or '')[:10]:>10}  {t}  {v['title']}")


def cmd_search(s, a):
    c = _conn(s)
    ch = store.find_channel(c, a.channel) if a.channel else None
    res = store.search(c, " ".join(a.query), channel_id=ch["channel_id"] if ch else None, limit=a.limit)
    if res.get("error"):
        sys.exit(f"search error: {res['error']}  (fts: {res['fts']})")
    o, cl = res["marks"]
    on, off = ("\033[1;33m", "\033[0m") if sys.stdout.isatty() else ("«", "»")
    for g in res["videos"]:
        print(f"\n▶ {g['title']}  — {g['channel_title']}")
        for h in g["hits"][:a.per_video]:
            snip = h["snippet"].replace(o, on).replace(cl, off)
            print(f"   [{export.ts(h['start'])}] https://youtu.be/{g['video_id']}?t={int(h['start'])}  {snip}")
    if not res["videos"]:
        print("no transcript hits")
    if res["titles"]:
        print("\ntitle/description matches: " + ", ".join(f"{t['title']} ({t['video_id']})" for t in res["titles"][:8]))


def cmd_import_takeout(s, a):
    from ingest.queue_builder import build_queue
    from ingest.sources import load_takeout
    signals, subs = load_takeout(Path(a.takeout))
    since = datetime.fromisoformat(a.since).replace(tzinfo=timezone.utc) if a.since else None
    items = build_queue(signals, include_music=not a.no_music, since=since)
    c = _conn(s)
    print(json.dumps(jobs.import_queue_items(c, items, lane=a.lane, limit=a.limit, transcripts=a.transcripts), indent=2))
    if a.follow_subscriptions:
        for sub in subs:
            jobs.enqueue(c, "scrape_channel", sub["channel_id"], args={"transcripts": "none"})
        print(f"queued catalogue scrapes for {len(subs)} subscriptions (no transcripts)")


def cmd_import_queue(s, a):
    from ingest.queue_builder import read_jsonl
    print(json.dumps(jobs.import_queue_items(_conn(s), read_jsonl(Path(a.queue)), lane=a.lane, limit=a.limit,
                                             transcripts=a.transcripts), indent=2))


def cmd_export(s, a):
    out = Path(a.out) if a.out else s.export_dir
    print(json.dumps(export.export_all(_conn(s), out, markdown=not a.no_markdown, parquet=not a.no_parquet,
                                       only_transcribed=not a.all), indent=2))


def cmd_status(s, a):
    c = _conn(s)
    print(json.dumps({"home": str(s.home), **store.stats(c), "jobs": jobs.summary(c)}, indent=2, default=str))


def cmd_retry(s, a):
    c = _conn(s)
    print(f"requeued {jobs.retry_failed(c)} failed jobs")
    if a.unpause:
        jobs.set_meta(c, "paused_until", None)


def cmd_rechunk(s, a):
    print(f"rechunked {store.rechunk_all(_conn(s), target_s=a.target)} transcripts")


def main(argv=None):
    p = argparse.ArgumentParser(prog="python -m kb", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--home", help="data folder (default $YTKB_HOME or ./ytkb-data)")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    x = sub.add_parser("serve", help="UI + worker")
    x.add_argument("--host", default="127.0.0.1")
    x.add_argument("--port", type=int, default=8765)
    x.add_argument("--no-worker", action="store_true")
    x.add_argument("--open", action="store_true", help="open the browser")
    x.set_defaults(fn=cmd_serve)

    x = sub.add_parser("add", help="add videos / channels (URLs, IDs, @handles)")
    x.add_argument("refs", nargs="*")
    x.add_argument("--file", help="text file of URLs, one per line")
    x.add_argument("--lane", default="inbox", choices=store.LANES)
    x.add_argument("--transcript", action="store_true", help="also fetch transcripts for added videos")
    x.add_argument("--channel-transcripts", default="all", help="for channel refs: none | all | top:N")
    x.add_argument("--run", action="store_true", help="drain the queue now")
    x.set_defaults(fn=cmd_add)

    x = sub.add_parser("follow", help="scrape channel catalogue(s) and queue transcripts")
    x.add_argument("refs", nargs="+", help="@handle, UC… id, or channel URL")
    x.add_argument("--transcripts", default="all", help="none | all | top:N (most viewed first)")
    x.add_argument("--min-duration", type=int, default=60, help="skip videos shorter than this (s)")
    x.add_argument("--shorts", action="store_true", help="include the Shorts tab")
    x.add_argument("--limit", type=int, help="cap uploads listed per tab")
    x.add_argument("--board", type=int, default=0, help="put the top-N breakout videos on the board")
    x.add_argument("--no-run", action="store_true", help="only queue; let `serve` or `work` process it")
    x.set_defaults(fn=cmd_follow)

    x = sub.add_parser("work", help="process queued jobs in the foreground")
    x.add_argument("--max", type=int)
    x.set_defaults(fn=cmd_work)

    x = sub.add_parser("top", help="a channel's videos ranked")
    x.add_argument("channel", help="@handle or UC id (already followed)")
    x.add_argument("--sort", default="views", choices=list(store.SORTS))
    x.add_argument("--kinds", default="video,live")
    x.add_argument("--min-duration", type=int)
    x.add_argument("--limit", type=int, default=25)
    x.set_defaults(fn=cmd_top)

    x = sub.add_parser("search", help="full-text search transcripts")
    x.add_argument("query", nargs="+")
    x.add_argument("--channel")
    x.add_argument("--limit", type=int, default=60)
    x.add_argument("--per-video", type=int, default=3)
    x.set_defaults(fn=cmd_search)

    x = sub.add_parser("import-takeout", help="Google Takeout folder -> board")
    x.add_argument("takeout")
    x.add_argument("--since")
    x.add_argument("--no-music", action="store_true")
    x.add_argument("--lane", default="inbox", choices=store.LANES)
    x.add_argument("--limit", type=int)
    x.add_argument("--transcripts", action="store_true")
    x.add_argument("--follow-subscriptions", action="store_true", help="also scrape every subscribed channel's catalogue")
    x.set_defaults(fn=cmd_import_takeout)

    x = sub.add_parser("import-queue", help="ingest/ queue.jsonl -> board")
    x.add_argument("queue")
    x.add_argument("--lane", default="inbox", choices=store.LANES)
    x.add_argument("--limit", type=int)
    x.add_argument("--transcripts", action="store_true")
    x.set_defaults(fn=cmd_import_queue)

    x = sub.add_parser("export", help="markdown + jsonl (+ parquet)")
    x.add_argument("--out")
    x.add_argument("--all", action="store_true", help="include videos without transcripts")
    x.add_argument("--no-markdown", action="store_true")
    x.add_argument("--no-parquet", action="store_true")
    x.set_defaults(fn=cmd_export)

    x = sub.add_parser("status")
    x.set_defaults(fn=cmd_status)

    x = sub.add_parser("retry", help="requeue failed jobs")
    x.add_argument("--unpause", action="store_true")
    x.set_defaults(fn=cmd_retry)

    x = sub.add_parser("rechunk", help="rebuild chunks from stored segments")
    x.add_argument("--target", type=float, default=60.0)
    x.set_defaults(fn=cmd_rechunk)

    a = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    a.fn(config.load(a.home), a)


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    main()
