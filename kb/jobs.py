"""Work queue + single polite worker.

Three job kinds: `meta` (video metadata), `transcript`, `scrape_channel`
(full catalogue + optional transcript fan-out). One worker thread is the
whole concurrency model on purpose: YouTube's caption endpoint punishes
parallelism from a single IP far more than it rewards it.

Failure policy
  Blocked (IP / bot check)  pause ALL YouTube-facing work for 10 min,
                            doubling per consecutive block up to 2 h; the
                            job keeps its place. 8 blocked attempts -> failed.
  Transient                 this job only: retry after 2^n min, 4 attempts.
  Terminal outcomes         (disabled / no_captions / unavailable) are
                            results, not failures: stored, never retried
                            automatically.
"""

from __future__ import annotations

import json
import logging
import random
import threading
import time
from datetime import datetime, timedelta, timezone

from kb import store, transcripts as T, urls, ytdlp_src
from kb.yt_api import ApiError, DataApi, QuotaExceeded

log = logging.getLogger("kb.jobs")

PRIO_CHANNEL_BULK, PRIO_BOARD, PRIO_MUST = 0, 10, 20
MAX_BLOCKED, MAX_TRANSIENT = 8, 4


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ------------------------------------------------------------------ queue ops
def enqueue(conn, kind: str, target: str, args: dict | None = None, priority: int = 0) -> bool:
    """Idempotent: a pending job for (kind, target) absorbs the new request
    (and is bumped to the higher priority)."""
    cur = conn.execute(
        "INSERT INTO jobs(kind, target, args, priority, updated_at) VALUES (?,?,?,?,?) "
        "ON CONFLICT(kind, target) WHERE status IN ('queued','running') "
        "DO UPDATE SET priority=MAX(priority, excluded.priority), "
        "args=COALESCE(excluded.args, jobs.args), updated_at=excluded.updated_at",
        (kind, target, json.dumps(args) if args else None, priority, store.now()))
    return cur.rowcount > 0


def enqueue_transcript(conn, video_id: str, priority: int = PRIO_BOARD, force: bool = False) -> bool:
    if not force:
        r = conn.execute("SELECT status FROM transcripts WHERE video_id=?", (video_id,)).fetchone()
        if r and r[0] in ("ok", "disabled", "no_captions", "unavailable"):
            return False
    store.ensure_video(conn, video_id)
    return enqueue(conn, "transcript", video_id, priority=priority)


def get_meta(conn, key, default=None):
    r = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return r[0] if r else default


def set_meta(conn, key, value):
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, None if value is None else str(value)))


def paused_until(conn) -> str | None:
    p = get_meta(conn, "paused_until")
    return p if p and p > _iso(_utcnow()) else None


def summary(conn) -> dict:
    counts = {f"{k}:{s}": n for k, s, n in conn.execute(
        "SELECT kind, status, COUNT(*) FROM jobs GROUP BY kind, status")}
    running = [dict(r) for r in conn.execute(
        "SELECT j.id, j.kind, j.target, v.title FROM jobs j LEFT JOIN videos v ON v.video_id=j.target "
        "WHERE j.status='running'")]
    failed = [dict(r) for r in conn.execute(
        "SELECT j.id, j.kind, j.target, j.error, j.attempts, v.title FROM jobs j "
        "LEFT JOIN videos v ON v.video_id=j.target WHERE j.status='failed' ORDER BY j.id DESC LIMIT 20")]
    return {"counts": counts,
            "queued": sum(n for k, n in counts.items() if k.endswith(":queued")),
            "running": running, "failed": failed, "paused_until": paused_until(conn),
            "api": get_meta(conn, "api_backend", "yt-dlp")}


def retry_failed(conn) -> int:
    return conn.execute("UPDATE jobs SET status='queued', attempts=0, not_before=NULL, error=NULL, "
                        "updated_at=? WHERE status='failed'", (store.now(),)).rowcount


def recover(conn) -> None:
    """A worker killed mid-job leaves 'running' rows; the (single) worker
    calls this on start so they run again."""
    conn.execute("UPDATE jobs SET status='queued' WHERE status='running'")


def claim(conn, allow_youtube: bool = True, allow_api_only: bool = True) -> dict | None:
    now = _iso(_utcnow())
    kinds = []
    if allow_youtube:
        kinds = ["meta", "transcript", "scrape_channel"]
    elif allow_api_only:
        kinds = ["meta", "scrape_channel"]
    if not kinds:
        return None
    q = ",".join("?" * len(kinds))
    row = conn.execute(
        f"UPDATE jobs SET status='running', attempts=attempts+1, updated_at=? WHERE id = ("
        f" SELECT id FROM jobs WHERE status='queued' AND (not_before IS NULL OR not_before <= ?)"
        f" AND kind IN ({q}) ORDER BY priority DESC, id LIMIT 1) RETURNING *",
        (now, now, *kinds)).fetchone()
    return dict(row) if row else None


def _finish(conn, job, status, error=None, result=None, not_before=None):
    conn.execute("UPDATE jobs SET status=?, error=?, result=?, not_before=?, updated_at=? WHERE id=?",
                 (status, error, json.dumps(result) if result is not None else None, not_before,
                  store.now(), job["id"]))


# --------------------------------------------------------------- metadata
class Meta:
    """Data API when a key is configured, yt-dlp otherwise -- and yt-dlp
    for the rest of the day once the API quota runs out."""

    def __init__(self, settings, api: DataApi | None = None, ytdlp=ytdlp_src):
        self.api = api if api is not None else (DataApi(settings.api_key) if settings.api_key else None)
        self.ytdlp = ytdlp
        self._api_off_until = 0.0

    @property
    def backend(self) -> str:
        return "api" if self._api_ok() else "yt-dlp"

    def _api_ok(self) -> bool:
        return self.api is not None and time.time() >= self._api_off_until

    def _quota(self, e):
        log.warning("Data API quota exhausted (%s); using yt-dlp for 6 h", e)
        self._api_off_until = time.time() + 6 * 3600

    def videos(self, ids: list[str]) -> list[dict]:
        if self._api_ok():
            try:
                return self.api.videos(ids)
            except QuotaExceeded as e:
                self._quota(e)
        out = []
        for vid in ids:
            v = self.ytdlp.video(vid)
            if v:
                out.append(v)
        return out

    def channel(self, ref: str, tabs=("videos", "streams"), limit: int | None = None):
        if self._api_ok():
            try:
                ch, vids = self.api.channel_catalogue(ref, limit=limit)
                kinds = {"videos": "video", "streams": "live", "shorts": "short"}
                keep = {kinds[t] for t in tabs} | ({"upcoming"} if "streams" in tabs else set())
                return ch, [v for v in vids if v["kind"] in keep]
            except QuotaExceeded as e:
                self._quota(e)
            except ApiError as e:
                log.warning("API could not resolve %s (%s); trying yt-dlp", ref, e)
        return self.ytdlp.channel_catalogue(ref, tabs=tabs, limit=limit)


# ------------------------------------------------------------------ worker
class Worker:
    def __init__(self, settings, conn, meta: Meta | None = None, tclient=None, sleep=time.sleep,
                 jitter=random.random):
        self.s, self.conn = settings, conn
        self.meta = meta or Meta(settings)
        self._tclient = tclient
        self.sleep, self.jitter = sleep, jitter
        self.processed = 0
        set_meta(conn, "api_backend", self.meta.backend)

    @property
    def tclient(self):
        if self._tclient is None:
            self._tclient = T.make_client(self.s)
        return self._tclient

    # -------------------------------------------------------------- loop
    def run(self, stop: threading.Event | None = None, max_jobs: int | None = None,
            exit_when_idle: bool = False, idle_poll: float = 2.0) -> int:
        recover(self.conn)
        stop = stop or threading.Event()
        while not stop.is_set():
            if max_jobs is not None and self.processed >= max_jobs:
                break
            did = self.run_once()
            if not did:
                if exit_when_idle:
                    nxt = self.conn.execute("SELECT MIN(COALESCE(not_before,'')) FROM jobs WHERE status='queued'").fetchone()[0]
                    if nxt is None:
                        break
                    wait = self._seconds_until(max(nxt, paused_until(self.conn) or ""))
                    if wait > 0:
                        log.info("next job not due for %.0fs; waiting", wait)
                        stop.wait(min(wait, 60))
                        continue
                stop.wait(idle_poll)
        return self.processed

    @staticmethod
    def _seconds_until(iso: str) -> float:
        if not iso:
            return 0
        return (datetime.fromisoformat(iso.replace("Z", "+00:00")) - _utcnow()).total_seconds()

    def run_once(self) -> bool:
        paused = paused_until(self.conn)
        job = claim(self.conn, allow_youtube=not paused, allow_api_only=self.meta.backend == "api")
        if not job:
            return False
        handler = getattr(self, f"_do_{job['kind']}")
        try:
            result = handler(job)
            _finish(self.conn, job, "done", result=result)
            set_meta(self.conn, "consecutive_blocks", 0)
        except T.Blocked as e:
            self._on_blocked(job, str(e))
        except Exception as e:  # noqa: BLE001 -- every failure is recorded on the job row
            self._on_transient(job, f"{type(e).__name__}: {str(e)[:400]}")
        self.processed += 1
        return True

    def _on_blocked(self, job, err):
        n = int(get_meta(self.conn, "consecutive_blocks", 0) or 0) + 1
        set_meta(self.conn, "consecutive_blocks", n)
        pause = min(600 * 2 ** (n - 1), 7200)
        until = _iso(_utcnow() + timedelta(seconds=pause))
        set_meta(self.conn, "paused_until", until)
        log.warning("YouTube blocked this IP (%s). Pausing YouTube work until %s.", err, until)
        if job["attempts"] >= MAX_BLOCKED:
            _finish(self.conn, job, "failed", error=err)
            if job["kind"] == "transcript":
                store.save_transcript(self.conn, T.Result("blocked", job["target"], error=err))
        else:
            _finish(self.conn, job, "queued", error=err, not_before=until)

    def _on_transient(self, job, err):
        log.warning("job %s %s %s failed: %s", job["id"], job["kind"], job["target"], err)
        if job["attempts"] >= MAX_TRANSIENT:
            _finish(self.conn, job, "failed", error=err)
            if job["kind"] == "transcript":
                store.save_transcript(self.conn, T.Result("error", job["target"], error=err))
        else:
            _finish(self.conn, job, "queued", error=err,
                    not_before=_iso(_utcnow() + timedelta(minutes=2 ** job["attempts"])))

    def _pace(self, base=None):
        self.sleep((self.s.delay if base is None else base) + 1.5 * self.jitter())

    # ------------------------------------------------------------ handlers
    def _do_transcript(self, job):
        vid = job["target"]
        try:
            res = T.fetch(vid, self.tclient, self.s.langs)
        finally:
            self._pace()
        store.save_transcript(self.conn, res)
        _archive(self.s, "transcripts", vid, {
            "video_id": vid, "status": res.status, "reason": res.reason, "language": res.language,
            "is_generated": res.is_generated, "segments": res.segments, "fetched_at": store.now()})
        if not self.conn.execute("SELECT title FROM videos WHERE video_id=?", (vid,)).fetchone()[0]:
            enqueue(self.conn, "meta", vid, priority=job["priority"])
        return {"status": res.status, "segments": len(res.segments), "language": res.language}

    def _do_meta(self, job):
        ids = [job["target"]]
        extra = []
        if self.meta.backend == "api":   # batch: one videos.list call covers 50 ids
            extra = [dict(r) for r in self.conn.execute(
                "UPDATE jobs SET status='running', attempts=attempts+1, updated_at=? WHERE id IN ("
                " SELECT id FROM jobs WHERE kind='meta' AND status='queued' AND id != ? "
                " AND (not_before IS NULL OR not_before <= ?) ORDER BY priority DESC, id LIMIT 49) RETURNING *",
                (store.now(), job["id"], store.now()))]
            ids += [j["target"] for j in extra]
        try:
            vids = self.meta.videos(ids)
            for v in vids:
                store.upsert_video(self.conn, v)
        except Exception:
            for j in extra:
                _finish(self.conn, j, "queued")
            raise
        finally:
            if self.meta.backend != "api":
                self._pace(1.0)
        found = {v["video_id"] for v in vids}
        for j in extra:
            _finish(self.conn, j, "done", result={"found": j["target"] in found, "batched_with": job["id"]})
        return {"found": len(found), "requested": len(ids)}

    def _do_scrape_channel(self, job):
        args = json.loads(job["args"] or "{}")
        tabs = tuple(args.get("tabs") or ("videos", "streams"))
        ch, vids = self.meta.channel(job["target"], tabs=tabs, limit=args.get("limit"))
        with store.tx(self.conn):
            store.upsert_channel(self.conn, ch)
            for v in vids:
                store.upsert_video(self.conn, {**v, "channel_id": v.get("channel_id") or ch["channel_id"]})
            self.conn.execute("UPDATE channels SET followed=1, last_scraped_at=? WHERE channel_id=?",
                              (store.now(), ch["channel_id"]))
        _archive(self.s, "channels", f"{ch['channel_id']}-{store.now()[:10]}", {"channel": ch, "videos": vids})
        queued = queue_channel_transcripts(self.conn, ch["channel_id"], args.get("transcripts", "none"),
                                           min_duration=args.get("min_duration", 60))
        board_n = int(args.get("board") or 0)
        if board_n:
            top = store.channel_videos(self.conn, ch["channel_id"], sort="breakout",
                                       kinds=("video", "live"), min_duration=args.get("min_duration", 60),
                                       limit=board_n)["videos"]
            for v in top:
                store.add_to_board(self.conn, v["video_id"], "inbox", priority=v["breakout"] or 0,
                                   sources=[f"channel:{ch['channel_id']}"])
        self._pace(1.0)
        return {"channel_id": ch["channel_id"], "videos": len(vids), "transcripts_queued": queued,
                "backend": self.meta.backend}


def queue_channel_transcripts(conn, channel_id: str, mode: str = "all", min_duration: int = 60,
                              kinds=("video", "live")) -> int:
    """mode: 'none' | 'all' | 'top:N' (by views). Most-viewed first, so an
    interrupted run has already captured the videos that matter most."""
    if not mode or mode == "none":
        return 0
    limit = int(mode.split(":", 1)[1]) if mode.startswith("top:") else None
    vids = store.channel_videos(conn, channel_id, sort="views", kinds=kinds, min_duration=min_duration,
                                limit=limit)["videos"]
    n = 0
    for v in vids:
        n += enqueue_transcript(conn, v["video_id"], priority=PRIO_CHANNEL_BULK)
    return n


def _archive(settings, kind: str, name: str, payload: dict) -> None:
    """Lossless JSON copy of what was fetched: the DB can always be rebuilt
    from raw/ without touching YouTube again."""
    try:
        d = settings.raw_dir / kind
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{name}.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except OSError as e:
        log.warning("archive write failed for %s/%s: %s", kind, name, e)


# ------------------------------------------------------------------ intake
def intake(conn, text: str, lane: str = "inbox", transcript: bool | None = None,
           follow: dict | None = None) -> dict:
    """Everything the add box / `kb add` accepts. Videos land on the board
    (and get metadata + a transcript job if wanted); channels get a scrape
    job. Must-watch cards always get a transcript."""
    refs, bad = urls.parse_many(text)
    out = {"videos": [], "channels": [], "unsupported": [], "unrecognised": bad}
    want_t = (lane == "must_watch") if transcript is None else transcript
    for r in refs:
        if r.kind == "video":
            new = store.add_to_board(conn, r.value, lane, sources=["manual"], top=True)
            if not conn.execute("SELECT title FROM videos WHERE video_id=?", (r.value,)).fetchone()[0]:
                enqueue(conn, "meta", r.value, priority=PRIO_MUST)
            if want_t:
                enqueue_transcript(conn, r.value, priority=PRIO_MUST if lane == "must_watch" else PRIO_BOARD)
            out["videos"].append({"video_id": r.value, "new": new})
        elif r.kind == "channel":
            opts = {"transcripts": "all", "tabs": ["videos", "streams"], "min_duration": 60, **(follow or {})}
            enqueue(conn, "scrape_channel", r.value, args=opts, priority=PRIO_BOARD)
            out["channels"].append({"ref": r.value, **opts})
        else:
            out["unsupported"].append(r.raw)
    return out


def import_queue_items(conn, items, lane: str = "inbox", limit: int | None = None,
                       transcripts: bool = False) -> dict:
    """Bridge from ingest/ (Takeout / RSS / URL lists): QueueItems arrive
    already de-duplicated and priority-sorted."""
    added = 0
    for it in items[:limit] if limit else items:
        with store.tx(conn):
            store.ensure_video(conn, it.video_id, title=it.title, channel_id=it.channel_id,
                               channel_title=it.channel, meta_source="takeout")
            if store.add_to_board(conn, it.video_id, lane, priority=it.priority, sources=it.sources, tags=it.tags):
                added += 1
            enqueue(conn, "meta", it.video_id, priority=PRIO_BOARD)
            if transcripts:
                enqueue_transcript(conn, it.video_id, priority=PRIO_BOARD)
    return {"items": len(items), "added": added}
