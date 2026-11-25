"""Tests for the Takeout / feed ingestion layer, against a synthetic
fixture shaped like a real Google Takeout export (no real person's data)."""

import json
from datetime import datetime, timezone
from pathlib import Path

from ingest import seed_queue
from ingest.queue_builder import build_queue, read_jsonl, stratified_sample, summarize
from ingest.sources import (discover_takeout, load_takeout, parse_channel_rss,
                            parse_playlist_csv, parse_url_list, parse_watch_history_html,
                            parse_watch_history_json)

FX = Path(__file__).parent / "fixtures"
TAKEOUT = FX / "takeout"
YT = TAKEOUT / "Takeout" / "YouTube and YouTube Music"
NOW = datetime(2026, 11, 24, tzinfo=timezone.utc)


def test_watch_history_json_skips_ads_and_removed():
    sigs = list(parse_watch_history_json(YT / "history" / "watch-history.json"))
    ids = [s.video_id for s in sigs]
    assert "SynthAd0001" not in ids                       # "From Google Ads"
    assert len(sigs) == 10                                 # 12 entries - ad - removed
    assert ids.count("SynthVid001") == 3                   # rewatches preserved as signals
    first = sigs[0]
    assert first.title == "Intro to Vector Databases" and first.channel == "Data Talks"
    assert first.channel_id == "UCsynthchan000000000001"
    assert first.at == datetime(2026, 11, 22, 21, 14, 3, 120000, tzinfo=timezone.utc)
    music = [s for s in sigs if s.extra.get("product") == "YouTube Music"]
    assert [s.video_id for s in music] == ["SynthVid004"]


def test_watch_history_html_format():
    sigs = list(parse_watch_history_html(FX / "watch-history.html"))
    assert [s.video_id for s in sigs] == ["SynthVid001", "SynthVid006"]
    assert sigs[1].title == "Q&A — Office Hours"      # entities unescaped
    assert sigs[1].at is not None                          # narrow-NBSP timestamp parsed
    assert sigs[0].at.hour == 21


def test_playlists_watch_later_and_named():
    wl = list(parse_playlist_csv(YT / "playlists" / "Watch later-videos.csv"))
    assert [s.video_id for s in wl] == ["SynthVid009", "SynthVid002", "SynthVid010"]
    assert {s.source for s in wl} == {"watch_later"}
    research = list(parse_playlist_csv(YT / "playlists" / "Research-videos.csv"))
    assert research[0].source == "playlist:Research"


def test_rss_feed_and_url_list():
    rss = list(parse_channel_rss((FX / "rss" / "UCsynthchan000000000001.xml").read_text()))
    assert [s.video_id for s in rss] == ["SynthVid012", "SynthVid001"]
    assert rss[0].channel == "Data Talks" and rss[0].source == "subscription_feed"
    urls = list(parse_url_list("# up next\nhttps://youtu.be/SynthVid013\nnot a url\nSynthVid014\n"))
    assert [s.video_id for s in urls] == ["SynthVid013", "SynthVid014"]


def test_discover_ignores_playlist_index_file():
    found = discover_takeout(TAKEOUT)
    assert len(found["history_json"]) == 1 and len(found["subscriptions"]) == 1
    assert sorted(p.name for p in found["playlists"]) == ["Research-videos.csv", "Watch later-videos.csv"]


def _queue(**kw):
    signals, _ = load_takeout(TAKEOUT)
    signals += list(parse_channel_rss((FX / "rss" / "UCsynthchan000000000001.xml").read_text()))
    return build_queue(signals, now=NOW, **kw)


def test_queue_dedupes_and_prioritizes():
    q = _queue()
    ids = [i.video_id for i in q]
    assert len(ids) == len(set(ids)) == 12
    by = {i.video_id: i for i in q}
    # Recent Watch Later items lead the queue.
    assert set(ids[:2]) == {"SynthVid009", "SynthVid002"}
    assert by["SynthVid002"].sources == ["watch_history", "watch_later"]
    # An old Watch Later item still outranks a fresh one-off watch.
    assert by["SynthVid010"].priority > by["SynthVid005"].priority
    # Rewatched x3 and in the subscription feed.
    assert by["SynthVid001"].watch_count == 3 and "subscription_feed" in by["SynthVid001"].sources
    # YouTube Music listens sink to the bottom half.
    assert ids.index("SynthVid004") > len(ids) // 2


def test_queue_tags_route_hints():
    by = {i.video_id: i for i in _queue()}
    assert "long_form" in by["SynthVid002"].tags        # podcast episode -> payload-size branch
    assert "music" in by["SynthVid003"].tags and "music" in by["SynthVid004"].tags
    assert "live" in by["SynthVid005"].tags
    assert "non_latin" in by["SynthVid006"].tags        # language-fallback branch
    assert "shorts" in by["SynthVid007"].tags
    assert by["SynthVid009"].tags == ["general"]        # Watch Later item with no metadata


def test_queue_filters():
    assert "SynthVid004" not in {i.video_id for i in _queue(include_music=False)}
    recent = _queue(since=datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert "SynthVid008" not in {i.video_id for i in recent}


def test_stratified_sample_covers_every_stratum():
    q = _queue()
    sample = stratified_sample(q, per_stratum=1)
    s = summarize(sample)
    assert set(s["by_source"]) == {"watch_history", "watch_later", "playlist", "subscription_feed"}
    assert {"music", "long_form", "live", "non_latin", "shorts", "general"} <= set(s["by_tag"])
    assert len(sample) < len(q)


def test_cli_build_and_dry_run(tmp_path, capsys):
    out, samp = tmp_path / "queue.jsonl", tmp_path / "sample.jsonl"
    seed_queue.main(["build", "--takeout", str(TAKEOUT), "--rss-dir", str(FX / "rss"),
                     "--out", str(out), "--sample", "1", "--sample-out", str(samp)])
    printed = capsys.readouterr().out
    assert json.loads(printed[: printed.index("}\n") + 1])["videos"] == 12
    assert len(read_jsonl(out)) == 12 and 0 < len(read_jsonl(samp)) < 12
    seed_queue.main(["submit", "--queue", str(samp), "--api", "https://example.invalid", "--dry-run"])
    assert "[dry-run] POST https://example.invalid/submissions" in capsys.readouterr().out
