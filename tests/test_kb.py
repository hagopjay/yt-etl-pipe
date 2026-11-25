"""Tests for the local knowledge store (kb/). No network: the transcript
client, Data API transport and yt-dlp extractor are all faked with the
shapes the real libraries return."""

import io
import json
import threading
import urllib.error
import urllib.request
from dataclasses import replace
from pathlib import Path

import pytest
from youtube_transcript_api import _errors as E

from kb import config, db, export, jobs, store, transcripts as T, urls, ytdlp_src
from kb.yt_api import DataApi, parse_duration

FX = Path(__file__).parent / "fixtures"
VID, VID2, VID3 = "AAAAAAAAAAA", "BBBBBBBBBBB", "CCCCCCCCCCC"
UC = "UCsynthchan0000000000001"  # real UC ids are 24 chars


# ---------------------------------------------------------------- fakes
class Snip:
    def __init__(self, start, duration, text):
        self.start, self.duration, self.text = start, duration, text


class Fetched:
    def __init__(self, snippets, lang="en", gen=False):
        self.snippets, self.language_code, self.is_generated = snippets, lang, gen


class Track:
    def __init__(self, lang, gen, snippets):
        self.language_code, self.is_generated, self._s = lang, gen, snippets

    def fetch(self):
        return Fetched(self._s, self.language_code, self.is_generated)


class TList:
    def __init__(self, tracks):
        self.tracks = tracks

    def __iter__(self):
        return iter(self.tracks)

    def _find(self, langs, gen):
        for l in langs:
            for t in self.tracks:
                if t.language_code == l and t.is_generated == gen:
                    return t
        raise E.NoTranscriptFound("x", langs, self)

    def find_manually_created_transcript(self, langs):
        return self._find(langs, False)

    def find_generated_transcript(self, langs):
        return self._find(langs, True)


def snippets(n=40, step=3.0, punct_every=7):
    return [Snip(i * step, step, f"word{i} &amp; more{'.' if i % punct_every == 6 else ''}") for i in range(n)]


class FakeClient:
    """behaviour: vid -> TList | Exception"""

    def __init__(self, behaviour):
        self.behaviour, self.calls = behaviour, []

    def list(self, vid):
        self.calls.append(vid)
        b = self.behaviour[vid]
        if isinstance(b, Exception):
            raise b
        return b


class FakeMeta:
    backend = "fake"

    def __init__(self, catalogue=None, videos=None):
        self.catalogue, self._videos = catalogue or {}, videos or {}

    def videos(self, ids):
        return [self._videos[i] for i in ids if i in self._videos]

    def channel(self, ref, tabs=("videos", "streams"), limit=None):
        return self.catalogue[ref]


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("YTKB_HOME", str(tmp_path / "home"))
    s = config.load()
    c = db.open_store(s.db_path)
    return s, c


def worker(env, client=None, meta=None):
    s, c = env
    return jobs.Worker(s, c, meta=meta or FakeMeta(), tclient=client, sleep=lambda *_: None, jitter=lambda: 0)


# ----------------------------------------------------------------- urls
@pytest.mark.parametrize("raw,kind,value", [
    ("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=1s", "video", "dQw4w9WgXcQ"),
    ("https://www.youtube.com/live/dQw4w9WgXcQ?si=x", "video", "dQw4w9WgXcQ"),
    ("https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ", "video", "dQw4w9WgXcQ"),
    ("dQw4w9WgXcQ", "video", "dQw4w9WgXcQ"),
    ("@ZacharyLLM", "channel", "@ZacharyLLM"),
    ("https://www.youtube.com/@ZacharyLLM/videos", "channel", "@ZacharyLLM"),
    (f"https://www.youtube.com/channel/{UC}", "channel", UC),
    (UC, "channel", UC),
    ("https://www.youtube.com/c/SomeCustom", "channel", "https://www.youtube.com/c/SomeCustom"),
    ("https://www.youtube.com/playlist?list=PLabc", "playlist", "PLabc"),
])
def test_classify(raw, kind, value):
    r = urls.classify(raw)
    assert (r.kind, r.value) == (kind, value)


def test_parse_many_dedupes_and_reports_junk():
    refs, bad = urls.parse_many("dQw4w9WgXcQ, https://youtu.be/dQw4w9WgXcQ\n@ZacharyLLM nonsense")
    assert [(r.kind, r.value) for r in refs] == [("video", "dQw4w9WgXcQ"), ("channel", "@ZacharyLLM")]
    assert bad == ["nonsense"]


# ---------------------------------------------------------- transcripts
def test_fetch_prefers_manual_and_cleans_text():
    tl = TList([Track("en", True, snippets(3)), Track("en", False, [Snip(0, 2, " Hello&#39;s\nworld ")])])
    r = T.fetch(VID, FakeClient({VID: tl}))
    assert r.status == "ok" and r.source == "caption_manual"
    assert r.segments == [{"start": 0.0, "duration": 2.0, "text": "Hello's world"}]


def test_fetch_falls_back_to_any_language_human_first():
    tl = TList([Track("fr", True, snippets(2)), Track("es", False, snippets(2))])
    r = T.fetch(VID, FakeClient({VID: tl}))
    assert r.language == "es" and r.is_generated is False


@pytest.mark.parametrize("exc,status,reason", [
    (E.TranscriptsDisabled(VID), "disabled", "disabled_by_creator"),
    (E.AgeRestricted(VID), "unavailable", "age_restricted"),
    (E.InvalidVideoId(VID), "unavailable", "invalid_id"),
    (E.VideoUnplayable(VID, "Members only", []), "unavailable", "unplayable"),
    (E.VideoUnavailable(VID), "unavailable", "unavailable"),
])
def test_terminal_outcomes_are_results_not_retries(exc, status, reason):
    r = T.fetch(VID, FakeClient({VID: exc}))
    assert (r.status, r.reason) == (status, reason)


def test_blocked_and_transient_raise():
    with pytest.raises(T.Blocked):
        T.fetch(VID, FakeClient({VID: E.IpBlocked(VID)}))
    with pytest.raises(T.Transient):
        T.fetch(VID, FakeClient({VID: E.PoTokenRequired(VID)}))


def test_chunking_respects_bounds_and_sentences():
    segs = [{"start": i * 3.0, "duration": 3.0, "text": f"w{i}" + ("." if i % 15 == 14 else "")} for i in range(100)]
    chunks = T.chunk(segs)
    assert chunks[0]["start"] == 0 and chunks[-1]["end"] == 300
    assert all(c["end"] - c["start"] <= 96 for c in chunks)
    assert chunks[0]["text"].endswith(".")                  # closed on a sentence past min_s
    joined = " ".join(c["text"] for c in chunks).split()
    assert joined == [s["text"] for s in segs]              # nothing lost or duplicated
    auto = [{"start": i * 3.0, "duration": 3.0, "text": f"w{i}"} for i in range(100)]
    assert all(55 <= c["end"] - c["start"] <= 66 for c in T.chunk(auto)[:-1])


# ---------------------------------------------------------------- board
def test_board_add_move_rank(env):
    _, c = env
    for v in (VID, VID2, VID3):
        store.add_to_board(c, v)
    assert [x["video_id"] for x in store.board(c)["inbox"]] == [VID, VID2, VID3]
    store.move(c, VID3, "inbox", after=VID, before=VID2)        # drag C between A and B
    assert [x["video_id"] for x in store.board(c)["inbox"]] == [VID, VID3, VID2]
    store.move(c, VID2, "must_watch")
    b = store.board(c)
    assert [x["video_id"] for x in b["must_watch"]] == [VID2] and len(b["inbox"]) == 2
    assert store.add_to_board(c, VID2, sources=["x"]) is False   # re-add never moves a placed card
    assert store.board(c)["must_watch"][0]["sources"] == ["x"]


def test_fractional_rank_renormalises_when_precision_runs_out(env):
    _, c = env
    store.add_to_board(c, VID)
    store.add_to_board(c, VID2)
    ids = [f"X{i:010d}" for i in range(80)]
    for i in ids:                       # keep inserting directly after VID: halves the gap 80 times
        store.add_to_board(c, i)
        store.move(c, i, "inbox", after=VID, before=store.board(c)["inbox"][1]["video_id"])
    order = [x["video_id"] for x in store.board(c)["inbox"]]
    assert order[0] == VID and order[-1] == VID2 and order[1:-1] == ids[::-1]


# --------------------------------------------------------------- worker
def test_worker_transcript_ok_is_stored_searchable_and_archived(env):
    s, c = env
    store.add_to_board(c, VID)
    store.upsert_video(c, {"video_id": VID, "title": "Agents from scratch", "channel_id": UC,
                           "channel_title": "Data Talks", "meta_source": "api", "duration_s": 120})
    jobs.enqueue_transcript(c, VID)
    w = worker(env, FakeClient({VID: TList([Track("en", False, snippets())])}))
    assert w.run_once()
    t = c.execute("SELECT * FROM transcripts WHERE video_id=?", (VID,)).fetchone()
    assert t["status"] == "ok" and t["segment_count"] == 40 and "word5 & more" in t["full_text"]
    assert c.execute("SELECT COUNT(*) FROM chunks WHERE video_id=?", (VID,)).fetchone()[0] >= 2
    hits = store.search(c, "word12")
    assert hits["videos"][0]["video_id"] == VID and hits["videos"][0]["hits"][0]["start"] <= 36
    assert "\x02" in hits["videos"][0]["hits"][0]["snippet"]
    assert (s.raw_dir / "transcripts" / f"{VID}.json").exists()
    assert not jobs.enqueue_transcript(c, VID)                    # done -> not re-queued
    assert store.board(c)["inbox"][0]["transcript_status"] == "ok"


def test_worker_blocked_pauses_everything_and_keeps_the_job(env):
    _, c = env
    jobs.enqueue_transcript(c, VID)
    jobs.enqueue_transcript(c, VID2)
    w = worker(env, FakeClient({VID: E.RequestBlocked(VID), VID2: TList([])}))
    assert w.run_once()
    assert jobs.paused_until(c) is not None
    job = c.execute("SELECT * FROM jobs WHERE target=?", (VID,)).fetchone()
    assert job["status"] == "queued" and job["not_before"] and "RequestBlocked" in job["error"]
    assert not w.run_once()                                       # paused: nothing YouTube-facing runs
    jobs.set_meta(c, "paused_until", None)
    c.execute("UPDATE jobs SET not_before=NULL")
    assert w.run_once()


def test_worker_transient_retries_then_records_error(env):
    _, c = env
    jobs.enqueue_transcript(c, VID)
    w = worker(env, FakeClient({VID: E.PoTokenRequired(VID)}))
    for _ in range(jobs.MAX_TRANSIENT):
        c.execute("UPDATE jobs SET not_before=NULL")
        assert w.run_once()
    assert c.execute("SELECT status FROM jobs").fetchone()[0] == "failed"
    assert c.execute("SELECT status FROM transcripts").fetchone()[0] == "error"
    assert jobs.retry_failed(c) == 1


def test_disabled_is_terminal_and_not_requeued(env):
    _, c = env
    jobs.enqueue_transcript(c, VID)
    worker(env, FakeClient({VID: E.TranscriptsDisabled(VID)})).run_once()
    assert c.execute("SELECT status FROM transcripts").fetchone()[0] == "disabled"
    assert not jobs.enqueue_transcript(c, VID)
    assert jobs.enqueue_transcript(c, VID, force=True)


def _catalogue():
    ch = {"channel_id": UC, "handle": "@datatalks", "title": "Data Talks", "subscriber_count": 1000,
          "meta_source": "fake"}
    vids = []
    for i, (views, dur, kind, date) in enumerate([
            (1_000, 600, "video", "2020-01-01T00:00:00Z"),
            (50_000, 1800, "video", "2026-11-01T00:00:00Z"),     # recent breakout
            (2_000, 900, "video", "2024-01-01T00:00:00Z"),
            (90_000, 30, "short", "2025-01-01T00:00:00Z"),
            (3_000, 7200, "live", "2025-06-01T00:00:00Z"),
            (500, 45, "video", "2025-06-01T00:00:00Z")]):          # under min_duration
        vids.append({"video_id": f"SynthCh{i:04d}", "title": f"video {i}", "duration_s": dur,
                     "view_count": views, "kind": kind, "published_at": date, "meta_source": "fake"})
    return ch, vids


def test_scrape_channel_stores_catalogue_and_queues_top_transcripts(env):
    _, c = env
    jobs.enqueue(c, "scrape_channel", "@datatalks", args={"transcripts": "top:2", "board": 1})
    w = worker(env, meta=FakeMeta(catalogue={"@datatalks": _catalogue()}))
    assert w.run_once()
    ch = store.find_channel(c, "@datatalks")
    assert ch["followed"] == 1 and ch["last_scraped_at"]
    queued = [r[0] for r in c.execute("SELECT target FROM jobs WHERE kind='transcript' ORDER BY id")]
    assert queued == ["SynthCh0001", "SynthCh0004"]              # most-viewed long-form first; short excluded
    data = store.channel_videos(c, UC, sort="breakout", kinds=("video",))
    assert data["videos"][0]["video_id"] == "SynthCh0001" and data["videos"][0]["breakout"] > 10
    vpd = store.channel_videos(c, UC, sort="views_per_day")["videos"]
    assert vpd[0]["video_id"] == "SynthCh0001"
    assert [x["video_id"] for x in store.board(c)["inbox"]] == ["SynthCh0001"]
    chans = store.channels(c)
    assert chans[0]["stored_videos"] == 6 and chans[0]["total_duration_s"] == 600 + 1800 + 900 + 30 + 7200 + 45


def test_meta_jobs_fill_in_video_rows(env):
    _, c = env
    out = jobs.intake(c, f"https://youtu.be/{VID} @someone junk", lane="must_watch")
    assert out["videos"][0]["video_id"] == VID and out["channels"][0]["ref"] == "@someone"
    assert out["unrecognised"] == ["junk"]
    kinds = sorted(r[0] for r in c.execute("SELECT kind FROM jobs"))
    assert kinds == ["meta", "scrape_channel", "transcript"]        # must_watch auto-queues transcript
    meta = FakeMeta(videos={VID: {"video_id": VID, "title": "Filled", "channel_id": UC,
                                  "channel_title": "Data Talks", "duration_s": 99, "meta_source": "fake"}})
    w = worker(env, FakeClient({VID: TList([Track("en", True, snippets(5))])}), meta=meta)
    c.execute("DELETE FROM jobs WHERE kind='scrape_channel'")
    while w.run_once():
        pass
    d = store.video_detail(c, VID)
    assert d["title"] == "Filled" and d["channel_title"] == "Data Talks" and d["transcript_status"] == "ok"


# ------------------------------------------------------------ backends
def test_iso_durations():
    assert parse_duration("PT1H2M3S") == 3723 and parse_duration("PT45S") == 45
    assert parse_duration("P1DT1M") == 86460 and parse_duration("P0D") == 0 and parse_duration("bad") is None


def test_data_api_catalogue_pages_and_normalises():
    pages = {
        "channels": {"items": [{"id": UC, "snippet": {"title": "Data Talks", "customUrl": "@datatalks"},
                                "statistics": {"subscriberCount": "1200", "videoCount": "51"},
                                "contentDetails": {"relatedPlaylists": {"uploads": "UUsynth"}}}]},
        "playlistItems:": {"items": [{"contentDetails": {"videoId": f"V{i:010d}"}} for i in range(50)],
                           "nextPageToken": "p2"},
        "playlistItems:p2": {"items": [{"contentDetails": {"videoId": "Vlast000000"}}]},
    }
    calls = []

    def opener(url, timeout=30):
        calls.append(url)
        ep = url.split("/v3/")[1].split("?")[0]
        if ep == "playlistItems":
            ep += ":" + ("p2" if "pageToken=p2" in url else "")
        if ep == "videos":
            ids = url.split("id=")[1].split("&")[0].split("%2C")
            body = {"items": [{"id": i, "snippet": {"title": "t #shorts" if i.endswith("1") else "t",
                                                    "channelId": UC, "publishedAt": "2026-01-01T00:00:00Z",
                                                    "liveBroadcastContent": "none"},
                               "contentDetails": {"duration": "PT40S" if i.endswith("1") else "PT12M", "caption": "true"},
                               "statistics": {"viewCount": "10"}} for i in ids]}
        else:
            body = pages[ep]
        return io.BytesIO(json.dumps(body).encode())

    api = DataApi("k", opener=opener)
    ch, vids = api.channel_catalogue("@datatalks")
    assert ch["uploads_playlist_id"] == "UUsynth" and ch["handle"] == "@datatalks" and ch["subscriber_count"] == 1200
    assert len(vids) == 51 and api.units == 1 + 2 + 2               # channels + 2 playlist pages + 2 videos batches
    assert vids[0]["duration_s"] == 720 and vids[0]["has_captions"] == 1
    assert {v["kind"] for v in vids if v["video_id"].endswith("1")} == {"short"}


def test_ytdlp_flat_catalogue_merges_tabs():
    infos = {
        "https://www.youtube.com/@datatalks/videos": {
            "channel_id": UC, "channel": "Data Talks", "uploader_id": "@datatalks", "channel_follower_count": 9,
            "entries": [{"id": "SynthVid001", "title": "a", "duration": 600, "view_count": 5, "timestamp": 1.7e9,
                         "url": "https://www.youtube.com/watch?v=SynthVid001"}]},
        "https://www.youtube.com/@datatalks/streams": {
            "channel_id": UC, "entries": [{"id": "SynthVid002", "title": "b", "duration": 7200, "view_count": 50,
                                           "live_status": "was_live"}]},
    }

    def extract(url):
        if url not in infos:
            raise RuntimeError("This channel does not have a shorts tab")
        return infos[url]

    ch, vids = ytdlp_src.channel_catalogue("@datatalks", tabs=("videos", "streams", "shorts"), extract=extract)
    assert ch["channel_id"] == UC and ch["handle"] == "@datatalks" and ch["video_count"] == 2
    assert [(v["video_id"], v["kind"], v["published_approx"]) for v in vids] == [
        ("SynthVid001", "video", 1), ("SynthVid002", "live", 0)]


# ------------------------------------------------------- ingest bridge
def test_import_takeout_fixture_into_board(env):
    from ingest.queue_builder import build_queue
    from ingest.sources import load_takeout
    _, c = env
    signals, _ = load_takeout(FX / "takeout")
    out = jobs.import_queue_items(c, build_queue(signals))
    assert out["added"] == out["items"] == 11
    inbox = store.board(c)["inbox"]
    assert {inbox[0]["video_id"], inbox[1]["video_id"]} == {"SynthVid009", "SynthVid002"}  # Watch Later leads
    assert next(x for x in inbox if x["video_id"] == "SynthVid001")["channel_title"] == "Data Talks"


# --------------------------------------------------------------- export
def test_export_markdown_and_jsonl(env, tmp_path):
    s, c = env
    store.upsert_video(c, {"video_id": VID, "title": "Agents: from scratch!", "channel_id": UC,
                           "channel_title": "Data Talks", "published_at": "2026-02-03T00:00:00Z",
                           "duration_s": 120, "view_count": 1234, "meta_source": "api"})
    store.add_to_board(c, VID, "must_watch")
    store.update_card(c, VID, note="Great bit on tool routing", interest=5)
    store.save_transcript(c, T.Result("ok", VID, "en", False,
                                      [{"start": float(i * 3), "duration": 3.0, "text": f"w{i}"} for i in range(40)]))
    rep = export.export_all(c, tmp_path / "out")
    md = next((tmp_path / "out" / "markdown").rglob("*.md")).read_text()
    assert md.startswith("---\nvideo_id: \"AAAAAAAAAAA\"") and "interest: 5" in md
    assert "Great bit on tool routing" in md and f"[1:00](https://www.youtube.com/watch?v={VID}&t=60s)" in md
    rows = [json.loads(x) for x in (tmp_path / "out" / "chunks.jsonl").read_text().splitlines()]
    assert rep["chunks_jsonl"] == len(rows) == 2 and rows[1]["url"].endswith("&t=60s")


# --------------------------------------------------------------- server
@pytest.fixture
def server(env):
    from http.server import ThreadingHTTPServer
    from kb.server import make_handler
    s, _ = env
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(s))
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def _req(url, body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def test_server_ui_api_and_csrf_guard(server):
    code, html = _req(server + "/")
    assert code == 200 and b"<title>ytkb" in html
    assert _req(server + "/api/add", {"text": VID})[0] == 403                    # no X-YTKB header
    assert _req(server + "/api/board", headers={"Host": "evil.example:80"})[0] == 403  # DNS rebinding
    code, body = _req(server + "/api/add", {"text": f"{VID} {VID2}", "lane": "must_watch"}, {"X-YTKB": "1"})
    assert code == 200 and len(json.loads(body)["videos"]) == 2
    code, body = _req(server + "/api/move", {"video_id": VID2, "lane": "must_watch", "before": VID}, {"X-YTKB": "1"})
    assert code == 200
    board = json.loads(_req(server + "/api/board")[1])
    assert [x["video_id"] for x in board["lanes"]["must_watch"]] == [VID2, VID]
    assert board["jobs"]["queued"] >= 2
    assert _req(server + "/api/video/ZZZZZZZZZZZ")[0] == 404
    code, body = _req(server + "/api/search?q=anything")
    assert code == 200 and json.loads(body)["videos"] == []
