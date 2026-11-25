"""Local web UI + JSON API (stdlib only) with the worker in a background
thread. `python -m kb serve` -> http://127.0.0.1:8765

Security posture for a no-auth localhost app:
  * binds 127.0.0.1 only
  * Host header must be localhost/127.0.0.1 -- blocks DNS-rebinding pages
    from reaching the API through a name that resolves to 127.0.0.1
  * every mutating request needs the `X-YTKB: 1` header; a custom header
    forces a CORS preflight this server never approves, so a random web
    page can't fire writes at it (simple-request CSRF)
"""

from __future__ import annotations

import json
import logging
import re
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from kb import db, export, jobs, store

log = logging.getLogger("kb.server")
UI = Path(__file__).with_name("ui") / "index.html"
_ALLOWED_HOSTS = re.compile(r"^(localhost|127\.0\.0\.1|\[::1\])(:\d+)?$")

ROUTES: list[tuple[str, re.Pattern, str]] = []


def route(method: str, pattern: str):
    def deco(fn):
        ROUTES.append((method, re.compile("^" + pattern + "$"), fn.__name__))
        return fn
    return deco


class Api:
    """Route handlers. Each gets (conn, match, query, body) and returns a
    JSON-able object."""

    def __init__(self, settings):
        self.s = settings

    @route("GET", r"/api/board")
    def board(self, c, m, q, b):
        return {"lanes": store.board(c), "stats": store.stats(c), "jobs": jobs.summary(c)}

    @route("GET", r"/api/status")
    def status(self, c, m, q, b):
        return {"stats": store.stats(c), "jobs": jobs.summary(c)}

    @route("POST", r"/api/add")
    def add(self, c, m, q, b):
        follow = {}
        mode = b.get("channel_transcripts")
        if mode:
            follow["transcripts"] = mode
        if b.get("include_shorts"):
            follow["tabs"] = ["videos", "streams", "shorts"]
        if b.get("board_top"):
            follow["board"] = int(b["board_top"])
        return jobs.intake(c, b.get("text", ""), lane=b.get("lane", "inbox"),
                           transcript=b.get("transcript"), follow=follow)

    @route("POST", r"/api/move")
    def move(self, c, m, q, b):
        r = store.move(c, b["video_id"], b["lane"], before=b.get("before"), after=b.get("after"))
        if b["lane"] == "must_watch":
            jobs.enqueue_transcript(c, b["video_id"], priority=jobs.PRIO_MUST)
        return {"rank": r}

    @route("GET", r"/api/video/(?P<vid>[A-Za-z0-9_-]{11})")
    def video(self, c, m, q, b):
        d = store.video_detail(c, m["vid"])
        if not d:
            raise LookupError("unknown video")
        return d

    @route("POST", r"/api/video/(?P<vid>[A-Za-z0-9_-]{11})")
    def update_video(self, c, m, q, b):
        store.update_card(c, m["vid"], **b)
        if b.get("lane") == "must_watch":
            jobs.enqueue_transcript(c, m["vid"], priority=jobs.PRIO_MUST)
        return store.video_detail(c, m["vid"])

    @route("POST", r"/api/video/(?P<vid>[A-Za-z0-9_-]{11})/remove")
    def remove_video(self, c, m, q, b):
        store.remove_card(c, m["vid"])
        return {"ok": True}

    @route("POST", r"/api/video/(?P<vid>[A-Za-z0-9_-]{11})/transcript")
    def want_transcript(self, c, m, q, b):
        return {"queued": jobs.enqueue_transcript(c, m["vid"], priority=jobs.PRIO_MUST, force=bool(b.get("force")))}

    @route("POST", r"/api/video/(?P<vid>[A-Za-z0-9_-]{11})/board")
    def to_board(self, c, m, q, b):
        new = store.add_to_board(c, m["vid"], b.get("lane", "inbox"), sources=b.get("sources") or ["manual"])
        return {"added": new}

    @route("GET", r"/api/video/(?P<vid>[A-Za-z0-9_-]{11})/markdown")
    def markdown(self, c, m, q, b):
        return {"markdown": export.markdown_for(c, m["vid"])}

    @route("GET", r"/api/channels")
    def channels(self, c, m, q, b):
        return {"channels": store.channels(c)}

    @route("GET", r"/api/channel/(?P<cid>UC[A-Za-z0-9_-]{10,40})")
    def channel(self, c, m, q, b):
        kinds = tuple(k for k in (q.get("kinds") or "video,live").split(",") if k)
        return store.channel_videos(c, m["cid"], sort=q.get("sort", "views"), kinds=kinds,
                                    min_duration=int(q.get("min_duration") or 0) or None,
                                    limit=int(q.get("limit") or 0) or None)

    @route("POST", r"/api/channel/(?P<cid>UC[A-Za-z0-9_-]{10,40})")
    def update_channel(self, c, m, q, b):
        store.set_channel(c, m["cid"], **b)
        return {"ok": True}

    @route("POST", r"/api/channel/(?P<cid>UC[A-Za-z0-9_-]{10,40})/scrape")
    def rescrape(self, c, m, q, b):
        args = {"transcripts": b.get("transcripts", "none"), "tabs": b.get("tabs") or ["videos", "streams"],
                "min_duration": int(b.get("min_duration", 60))}
        return {"queued": jobs.enqueue(c, "scrape_channel", m["cid"], args=args, priority=jobs.PRIO_BOARD)}

    @route("POST", r"/api/channel/(?P<cid>UC[A-Za-z0-9_-]{10,40})/transcripts")
    def channel_transcripts(self, c, m, q, b):
        if b.get("video_ids"):
            n = sum(jobs.enqueue_transcript(c, v, priority=jobs.PRIO_BOARD) for v in b["video_ids"])
        else:
            n = jobs.queue_channel_transcripts(c, m["cid"], b.get("mode", "all"),
                                               min_duration=int(b.get("min_duration", 60)),
                                               kinds=tuple(b.get("kinds") or ("video", "live")))
        return {"queued": n}

    @route("GET", r"/api/search")
    def search(self, c, m, q, b):
        return store.search(c, q.get("q", ""), channel_id=q.get("channel") or None,
                            limit=int(q.get("limit") or 60))

    @route("GET", r"/api/jobs")
    def job_list(self, c, m, q, b):
        return jobs.summary(c)

    @route("POST", r"/api/jobs/retry")
    def retry(self, c, m, q, b):
        return {"requeued": jobs.retry_failed(c)}

    @route("POST", r"/api/jobs/unpause")
    def unpause(self, c, m, q, b):
        jobs.set_meta(c, "paused_until", None)
        return {"ok": True}

    @route("POST", r"/api/export")
    def do_export(self, c, m, q, b):
        return export.export_all(c, self.s.export_dir)


def make_handler(settings):
    api = Api(settings)
    local = threading.local()

    def conn():
        if not hasattr(local, "c"):
            local.c = db.open_store(settings.db_path)
        return local.c

    class Handler(BaseHTTPRequestHandler):
        server_version = "ytkb"

        def log_message(self, fmt, *args):
            log.debug("%s " + fmt, self.address_string(), *args)

        def _send(self, code, payload=None, ctype="application/json; charset=utf-8", raw: bytes | None = None):
            body = raw if raw is not None else json.dumps(payload, ensure_ascii=False, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(body)

        def _dispatch(self, method):
            if not _ALLOWED_HOSTS.match(self.headers.get("Host", "")):
                return self._send(HTTPStatus.FORBIDDEN, {"error": "bad host"})
            u = urlparse(self.path)
            if method == "GET" and u.path in ("/", "/index.html"):
                return self._send(200, raw=UI.read_bytes(), ctype="text/html; charset=utf-8")
            if method == "POST" and self.headers.get("X-YTKB") != "1":
                return self._send(HTTPStatus.FORBIDDEN, {"error": "missing X-YTKB header"})
            body = {}
            if method == "POST":
                n = int(self.headers.get("Content-Length") or 0)
                if n > 2_000_000:
                    return self._send(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "too large"})
                try:
                    body = json.loads(self.rfile.read(n) or b"{}")
                except json.JSONDecodeError:
                    return self._send(400, {"error": "invalid JSON"})
            q = {k: v[-1] for k, v in parse_qs(u.query).items()}
            for meth, rx, name in ROUTES:
                if meth == method and (m := rx.match(u.path)):
                    try:
                        return self._send(200, getattr(api, name)(conn(), m.groupdict(), q, body))
                    except LookupError as e:
                        return self._send(404, {"error": str(e)})
                    except (ValueError, KeyError, TypeError) as e:
                        return self._send(400, {"error": f"{type(e).__name__}: {e}"})
                    except Exception as e:  # noqa: BLE001
                        log.exception("handler %s failed", name)
                        return self._send(500, {"error": f"{type(e).__name__}: {e}"})
            self._send(404, {"error": "not found"})

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

    return Handler


def serve(settings, host="127.0.0.1", port=8765, worker=True, open_browser=False):
    db.open_store(settings.db_path).close()
    httpd = ThreadingHTTPServer((host, port), make_handler(settings))
    stop = threading.Event()
    t = None
    if worker:
        def run_worker():
            w = jobs.Worker(settings, db.open_store(settings.db_path))
            log.info("worker started (metadata backend: %s)", w.meta.backend)
            w.run(stop=stop)
        t = threading.Thread(target=run_worker, name="kb-worker", daemon=True)
        t.start()
    url = f"http://{host}:{httpd.server_address[1]}/"
    from kb.yt_api import mask
    meta = f"Data API, key {mask(settings.api_key)}" if settings.api_key else "yt-dlp (no YT_API_KEY)"
    print(f"ytkb UI  ->  {url}   (data: {settings.home})   Ctrl-C to stop")
    print(f"metadata ->  {meta}.  Verify with: python -m kb check")
    if open_browser:
        import webbrowser
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        httpd.server_close()
