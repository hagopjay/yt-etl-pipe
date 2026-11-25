# ingest/ — seed the transcript queue from real viewing activity

Turns a person's YouTube activity into a prioritized, de-duplicated
queue, then feeds it to `POST /submissions` at a pace the pipeline can
absorb. Built to test every layer with realistic input rather than
hand-picked URLs.

## Getting the data

The YouTube Data API returns nothing for watch history or Watch Later
(it has returned the placeholder IDs `HL` / `WL` since 2016), and the
home feed was never exposed. So:

| Signal | Source | File |
|---|---|---|
| Watch history | Google Takeout → *YouTube and YouTube Music* → history (choose **JSON**) | `history/watch-history.json` (HTML also parsed) |
| Queue (Watch Later) | Takeout → playlists | `playlists/Watch later-videos.csv` |
| Saved playlists | Takeout → playlists | `playlists/<Name>-videos.csv` |
| Subscriptions feed | Takeout → subscriptions, then each channel's public RSS | `subscriptions/subscriptions.csv` + `--fetch-rss` |
| Anything else ("up next", a list from a chat) | plain text, one URL/ID per line | `--urls file.txt` |

Get the export owner's OK before using it — watch history is personal data.
Keep the export out of the repo (`.gitignore` it) and out of the
landing bucket; only video IDs need to leave the machine.

## Use

```bash
# Build the full queue + a stratified test sample (N per source/tag stratum)
python -m ingest.seed_queue build --takeout ~/Downloads/Takeout \
    --fetch-rss --since 2026-01-01 --out queue.jsonl --sample 4 --sample-out sample.jsonl

# Check what would be sent
python -m ingest.seed_queue submit --queue sample.jsonl --api "$API" --dry-run

# Send it, one every 20 s, poll until each reaches a terminal status
python -m ingest.seed_queue submit --queue sample.jsonl --api "$API" --poll --results results.csv
```

`results.csv` is the layer-coverage report: one row per video with its
heuristic tags (`music`, `long_form`, `live`, `non_latin`, `shorts`) and
the final pipeline status, so you can see which branches were actually hit.

## Priority score

Watch Later +100 · each rewatch +15 (max 4) · new subscription upload +40 ·
other saved playlist +20 · recency 0–30 (30-day half-life) · YouTube Music −25.
