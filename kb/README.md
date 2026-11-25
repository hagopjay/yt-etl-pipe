# kb/ — local YouTube knowledge store

A must-watch board, channel catalogues ranked by real popularity, and every
transcript you care about in one SQLite file with full-text search that
deep-links to the second. Runs on your machine: a home IP fetches captions
without the residential proxy the AWS pipeline needs, and it costs nothing.

```
pip install -r requirements-kb.txt
python -m kb serve --open                 # http://127.0.0.1:8765
```

Optional: a YouTube Data API v3 key for exact dates, likes, captions flags
and quota-cheap catalogue scrapes. Without one, everything still works through
yt-dlp. Put it in a `.env` file in the folder you run from (git-ignored):

```
YT_API_KEY=AIza...
```

then `python -m kb check` (1 quota unit). It prints `✓ works` with a channel
looked up through your key, or names the exact problem: invalid key, API not
enabled, wrong key restrictions, or quota used up. Restart `serve` after adding
the key; the startup banner shows which backend is active.

**The key does not fetch transcripts.** The Data API only downloads captions
for videos you own (OAuth), so transcripts always come from YouTube's caption
endpoint, rate-limited per IP. The key moves catalogue and metadata traffic off
that IP, and that work keeps running while transcripts are paused.

## The loop

1. **Paste** anything into the add bar: video URLs, IDs, `@handles`,
   channel URLs, several at once. Videos land in **Inbox** (or straight in
   **Must-watch**); channels get scraped.
2. **Curate** by dragging cards between Inbox → Must-watch → Watching →
   Done / Skip, and up or down to rank. Dropping a card into Must-watch queues its
   transcript automatically. Click a card for the player, ★ interest, notes and
   the transcript; click any timestamp to jump the embedded player there.
3. **Follow a channel you like.** Its whole catalogue is listed with length,
   views, **views/day** (normalises for age) and **× median** (how far a video
   beat that channel's typical upload: the cleanest "this one landed" signal).
   Transcripts queue most-viewed first, so an interrupted run has already
   captured the ones that matter.
4. **Search** every transcript (`/`). Hits are grouped by video with
   timestamps; clicking one opens the video at that moment.
5. **Export** (queue panel → Export, or `python -m kb export`): Obsidian-ready
   Markdown with front-matter + `[mm:ss](link)` paragraphs, and `chunks.jsonl`
   ready for an embedding / RAG job.

## CLI

```bash
python -m kb follow @ZacharyLLM                          # catalogue + ALL transcripts (default)
python -m kb follow @SomeChannel --transcripts top:25 --min-duration 300 --board 10
python -m kb top @ZacharyLLM --sort breakout             # views | views_per_day | breakout | longest | newest
python -m kb add https://youtu.be/ID ... --lane must_watch
python -m kb search "tool routing" --channel @ZacharyLLM
python -m kb import-takeout ~/Downloads/Takeout --since 2026-01-01   # via ingest/
python -m kb import-queue queue.jsonl                    # output of ingest/seed_queue.py build
python -m kb work                                        # drain the queue without the UI
python -m kb export                                      # -> ytkb-data/exports/
python -m kb check                                       # verify YT_API_KEY + caption endpoint
python -m kb status | retry --unpause | rechunk --target 45
```

## Storage layout (`$YTKB_HOME`, default `./ytkb-data`, git-ignored)

```
kb.sqlite3          the store (WAL mode; safe to read while the server runs)
raw/transcripts/    lossless JSON per fetch (segments as returned)
raw/channels/       each catalogue scrape, dated
exports/            markdown/, chunks.jsonl, videos.jsonl, *.parquet
```

| table | what | yours or YouTube's |
|---|---|---|
| `channels`, `videos` | metadata; NULLs never overwrite known values, so thin sources (RSS, Takeout) can't erase API data | YouTube's, refreshable |
| `transcripts` | one row per video: `ok` / `disabled` / `no_captions` / `unavailable` / `blocked` / `error`, plus language, human vs auto, word count, full text | fetched |
| `segments` | caption lines exactly as fetched (2–5 s each) | fetched, lossless |
| `chunks` + `chunks_fts` | ~60 s windows closed on sentence ends where captions have punctuation; the unit of search and of RAG retrieval; rebuildable from segments (`rechunk`) | derived |
| `curation` | lane, fractional rank (one UPDATE per drag), interest, note, sources | **yours** |
| `jobs` | queue: `meta`, `transcript`, `scrape_channel`; one pending job per target | runtime |

Query it directly, e.g. `sqlite3 ytkb-data/kb.sqlite3` or DuckDB's sqlite
scanner. Example: the 20 longest must-watch videos still without a transcript:

```sql
SELECT v.title, v.duration_s/60 AS min FROM curation c JOIN videos v USING(video_id)
LEFT JOIN transcripts t USING(video_id)
WHERE c.lane='must_watch' AND t.video_id IS NULL ORDER BY v.duration_s DESC LIMIT 20;
```

## Being a good citizen (and not getting blocked)

One worker, one request at a time, `YTKB_DELAY` (default 2.5 s) + jitter
between transcript fetches. If YouTube blocks the IP, all YouTube-facing work
pauses for 10 min, doubling up to 2 h, and the header pill turns red; jobs keep
their place. Captions a creator **disabled** are recorded and never retried.
Age-restricted, members-only, private and deleted videos are recorded as
`unavailable` instead of being retried (the AWS `fetch_captions` gets this wrong;
see `tests/test_findings_pipeline.py`). For personal and research use; the
legal posture in `lambda/fetch_captions.py` applies here too.

## Environment

Set these in the shell or in a `.env` file (cwd first, then `$YTKB_HOME/.env`). Real environment variables win.

| var | default | |
|---|---|---|
| `YTKB_HOME` | `./ytkb-data` | data folder |
| `YT_API_KEY` | — | Data API v3. Falls back to yt-dlp for 6 h when quota runs out, and for the whole run if the key is broken (logged loudly) |
| `YTKB_LANGS` | `en,en-US,en-GB` | preferred caption languages; otherwise human-made in any language, then auto |
| `YTKB_DELAY` | `2.5` | seconds between transcript fetches |
| `YTKB_PROXY_USER` / `YTKB_PROXY_PASS` | — | Webshare rotating residential; only needed from cloud IPs |
| `YTKB_PROXY_URL` | — | any other http(s) proxy |

## Security

The server binds `127.0.0.1` only, rejects any `Host` header that isn't
localhost (DNS rebinding), and requires an `X-YTKB: 1` header on every write.
A custom header forces a CORS preflight that this server never grants, so other
web pages can't post to it. There is no auth, so don't expose it beyond localhost.
