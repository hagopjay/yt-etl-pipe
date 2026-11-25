#!/usr/bin/env python3
"""
ingest/seed_queue.py -- build a transcript queue from someone's YouTube
activity and feed it to the deployed pipeline at a safe pace.

  # 1. Build: Takeout folder (+ optional extra URL list / live RSS feed)
  python -m ingest.seed_queue build --takeout ~/Takeout --out queue.jsonl \
      --sample 4 --sample-out sample.jsonl [--fetch-rss] [--since 2026-01-01]

  # 2. Submit the stratified sample, one every 20 s, and track outcomes
  python -m ingest.seed_queue submit --queue sample.jsonl \
      --api https://abc.execute-api.us-east-1.amazonaws.com --poll --results results.csv

Pacing matters: fetch_captions has reserved concurrency 3 and the
FetchCaptions Retry block does not cover Lambda throttling, so a burst
of submissions turns into BLOCKED_PERMANENTLY rows (see the assessment).
The default --interval keeps well under that until it's fixed.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ingest.queue_builder import (build_queue, read_jsonl, stratified_sample,  # noqa: E402
                                  summarize, write_jsonl)
from ingest.sources import (load_takeout, parse_channel_rss, parse_url_list,  # noqa: E402
                            rss_url)

TERMINAL = {"COMPLETED", "NO_CAPTIONS", "UNAVAILABLE", "REJECTED", "REVIEW_EXPIRED",
            "BLOCKED_PERMANENTLY", "TRANSCRIBE_FAILED"}


def _http(method: str, url: str, body: dict | None = None, retries: int = 4) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < retries:
                time.sleep(2 ** attempt)
                continue
            try:
                return e.code, json.loads(e.read() or b"{}")
            except json.JSONDecodeError:
                return e.code, {}
        except urllib.error.URLError:
            if attempt < retries:
                time.sleep(2 ** attempt)
                continue
            raise
    return 0, {}


def cmd_build(a) -> None:
    signals, subs = load_takeout(Path(a.takeout)) if a.takeout else ([], [])
    if a.urls:
        signals.extend(parse_url_list(Path(a.urls).read_text(encoding="utf-8")))
    if a.rss_dir:  # pre-downloaded feeds, one XML per channel
        for p in Path(a.rss_dir).glob("*.xml"):
            signals.extend(parse_channel_rss(p.read_text(encoding="utf-8")))
    if a.fetch_rss and subs:
        for s in subs[: a.max_channels]:
            try:
                with urllib.request.urlopen(rss_url(s["channel_id"]), timeout=15) as r:
                    signals.extend(parse_channel_rss(r.read().decode("utf-8")))
            except Exception as e:  # one dead channel shouldn't sink the build
                print(f"  rss skip {s['channel_id']}: {e}", file=sys.stderr)
            time.sleep(0.5)
    since = datetime.fromisoformat(a.since).replace(tzinfo=timezone.utc) if a.since else None
    items = build_queue(signals, include_music=not a.no_music, since=since)
    write_jsonl(items, Path(a.out))
    print(json.dumps({"signals": len(signals), "subscriptions": len(subs), **summarize(items)}, indent=2))
    if a.sample:
        sample = stratified_sample(items, per_stratum=a.sample)
        write_jsonl(sample, Path(a.sample_out))
        print(f"stratified sample: {len(sample)} videos -> {a.sample_out}")
        print(json.dumps(summarize(sample), indent=2))


def cmd_submit(a) -> None:
    items = read_jsonl(Path(a.queue))[: a.max]
    api = a.api.rstrip("/")
    rows, started = [], {}
    for i, it in enumerate(items):
        if a.dry_run:
            print(f"[dry-run] POST {api}/submissions  {it.url}  tags={it.tags} p={it.priority}")
            continue
        code, body = _http("POST", f"{api}/submissions", {"url": it.url})
        status = body.get("status", f"HTTP_{code}")
        print(f"[{i + 1}/{len(items)}] {it.video_id} -> {code} {status}")
        started[it.video_id] = (time.time(), it, status)
        if i < len(items) - 1:
            time.sleep(a.interval)
    if a.dry_run:
        return

    deadline = time.time() + a.poll_minutes * 60
    final = {vid: s for vid, (_, _, s) in started.items()}
    while a.poll and time.time() < deadline and any(s not in TERMINAL for s in final.values()):
        for vid, s in list(final.items()):
            if s in TERMINAL:
                continue
            code, body = _http("GET", f"{api}/submissions/{vid}")
            if code == 200:
                final[vid] = body.get("status", s)
        time.sleep(15)

    for vid, (t0, it, first) in started.items():
        rows.append({"video_id": vid, "title": it.title, "tags": "|".join(it.tags),
                     "sources": "|".join(it.sources), "first_response": first,
                     "final_status": final[vid], "elapsed_s": round(time.time() - t0)})
    with Path(a.results).open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else ["video_id"])
        w.writeheader()
        w.writerows(rows)
    by_status: dict[str, int] = {}
    for r in rows:
        by_status[r["final_status"]] = by_status.get(r["final_status"], 0) + 1
    print(json.dumps({"results": a.results, "by_final_status": by_status}, indent=2))


def main(argv=None) -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build", help="build queue.jsonl from Takeout / URL lists / RSS")
    b.add_argument("--takeout", help="extracted Takeout folder (or any folder containing its files)")
    b.add_argument("--urls", help="text file, one URL or video ID per line")
    b.add_argument("--rss-dir", help="folder of saved channel RSS XML files")
    b.add_argument("--fetch-rss", action="store_true", help="fetch live RSS for every subscription")
    b.add_argument("--max-channels", type=int, default=200)
    b.add_argument("--since", help="ignore activity before YYYY-MM-DD")
    b.add_argument("--no-music", action="store_true", help="drop YouTube Music listens (unless in Watch Later)")
    b.add_argument("--out", default="queue.jsonl")
    b.add_argument("--sample", type=int, default=0, help="videos per stratum for a test sample")
    b.add_argument("--sample-out", default="sample.jsonl")
    b.set_defaults(fn=cmd_build)

    s = sub.add_parser("submit", help="POST queue items to the pipeline API, paced")
    s.add_argument("--queue", required=True)
    s.add_argument("--api", required=True, help="api_base_url Terraform output")
    s.add_argument("--interval", type=float, default=20.0, help="seconds between submissions")
    s.add_argument("--max", type=int, default=50)
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--poll", action="store_true", help="poll GET /submissions/{id} until terminal")
    s.add_argument("--poll-minutes", type=float, default=45)
    s.add_argument("--results", default="results.csv")
    s.set_defaults(fn=cmd_submit)

    a = p.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
