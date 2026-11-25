"""YouTube Data API v3 backend (stdlib urllib, no google client library).

Quota math (10,000 units/day free):
  channels.list            1 unit    resolve @handle / UC id -> uploads playlist
  playlistItems.list       1 unit    per 50 uploads (ids only)
  videos.list              1 unit    per 50 videos (snippet+contentDetails+statistics)
  search.list            100 units   only for legacy /c/custom URLs

So a full catalogue of a 500-video channel costs ~21 units; you could
scrape ~450 such channels a day. Sorting by popularity happens locally
after the fetch, because search.list(order=viewCount) is 100x the cost
and caps at 500 results anyway.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Iterator

BASE = "https://www.googleapis.com/youtube/v3/"
_DUR = re.compile(r"^P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?)?$")


class ApiError(Exception):
    def __init__(self, msg: str, status: int | None = None, reasons: tuple[str, ...] = (), message: str = ""):
        super().__init__(msg)
        self.status, self.reasons, self.message = status, reasons, message


class QuotaExceeded(ApiError):
    pass


def _google_error(body: str) -> tuple[tuple[str, ...], str]:
    """Google errors carry machine reasons in two places: the legacy
    error.errors[].reason (keyInvalid, quotaExceeded, accessNotConfigured)
    and google.rpc.ErrorInfo details[].reason (API_KEY_INVALID,
    SERVICE_DISABLED, API_KEY_HTTP_REFERRER_BLOCKED, ...). Collect both."""
    try:
        err = json.loads(body).get("error", {})
    except (json.JSONDecodeError, AttributeError):
        return (), body[:200]
    reasons = [e.get("reason") for e in err.get("errors", [])] + [d.get("reason") for d in err.get("details", [])]
    return tuple(r for r in reasons if r), err.get("message", "")


# reason -> (what it means, what to do). Ordered: first match wins.
DIAGNOSES = [
    ({"API_KEY_INVALID", "keyInvalid"},
     "the key is not valid", "copy it again from Google Cloud Console > APIs & Services > Credentials "
     "(no quotes or spaces), or create a new one"),
    ({"SERVICE_DISABLED", "accessNotConfigured"},
     "YouTube Data API v3 is not enabled for this key's project",
     "Console > APIs & Services > Library > 'YouTube Data API v3' > Enable (takes a minute to propagate)"),
    ({"API_KEY_HTTP_REFERRER_BLOCKED", "ipRefererBlocked"},
     "the key has a website (HTTP referrer) restriction", "a script sends no referrer: set Application "
     "restrictions to None (or IP addresses) on the key"),
    ({"API_KEY_IP_ADDRESS_BLOCKED"},
     "the key is restricted to other IP addresses", "add your current public IP to the key's restrictions, or remove them"),
    ({"API_KEY_SERVICE_BLOCKED", "API_KEY_ANDROID_APP_BLOCKED", "API_KEY_IOS_APP_BLOCKED"},
     "the key's API restrictions don't include YouTube Data API v3",
     "edit the key > API restrictions > add 'YouTube Data API v3'"),
    ({"quotaExceeded", "dailyLimitExceeded", "RATE_LIMIT_EXCEEDED", "rateLimitExceeded"},
     "the daily quota (10,000 units) is used up", "it resets at midnight Pacific time; kb falls back to yt-dlp until then"),
]


def diagnose(e: ApiError) -> tuple[str, str]:
    for keys, what, fix in DIAGNOSES:
        if keys & set(e.reasons):
            return what, fix
    return f"HTTP {e.status}: {e.message or e}", "see the message above"


def parse_duration(iso: str | None) -> int | None:
    if not iso:
        return None
    m = _DUR.match(iso)
    if not m:
        return None
    d, h, mi, s = m.groups()
    return int(d or 0) * 86400 + int(h or 0) * 3600 + int(mi or 0) * 60 + int(float(s or 0))


def _int(x):
    try:
        return int(x)
    except (TypeError, ValueError):
        return None


def _thumb(snippet: dict) -> str | None:
    th = snippet.get("thumbnails") or {}
    for k in ("medium", "high", "default"):
        if k in th:
            return th[k].get("url")
    return None


def classify_kind(duration_s: int | None, title: str, description: str, live: str | None,
                  had_live_details: bool) -> str:
    if live in ("live", "upcoming"):
        return live
    if had_live_details:
        return "live"            # a finished livestream (VOD of a stream)
    text = f"{title} {description}".lower()
    if duration_s is not None and (duration_s <= 60 or (duration_s <= 180 and "#short" in text)):
        return "short"
    return "video"


class DataApi:
    def __init__(self, key: str, opener=None):
        self.key = key
        self.units = 0
        self._open = opener or urllib.request.urlopen

    def _get(self, endpoint: str, cost: int = 1, **params) -> dict:
        params = {k: v for k, v in params.items() if v is not None}
        params["key"] = self.key
        url = BASE + endpoint + "?" + urllib.parse.urlencode(params)
        try:
            with self._open(url, timeout=30) as r:
                self.units += cost
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            reasons, message = _google_error(body)
            cls = QuotaExceeded if {"quotaExceeded", "dailyLimitExceeded"} & set(reasons) else ApiError
            raise cls(f"{endpoint} HTTP {e.code} {','.join(reasons)}: {message or body[:300]}",
                      status=e.code, reasons=reasons, message=message) from e

    # ------------------------------------------------------------ channels
    def resolve_channel(self, ref: str) -> dict | None:
        part = "snippet,statistics,contentDetails"
        if ref.startswith("UC"):
            data = self._get("channels", part=part, id=ref)
        elif ref.startswith("@"):
            data = self._get("channels", part=part, forHandle=ref)
        elif "/user/" in ref:
            data = self._get("channels", part=part, forUsername=ref.rstrip("/").rsplit("/", 1)[-1])
        else:  # legacy /c/custom -- no direct lookup exists
            q = ref.rstrip("/").rsplit("/", 1)[-1]
            hit = self._get("search", cost=100, part="snippet", type="channel", q=q, maxResults=1)
            items = hit.get("items") or []
            if not items:
                return None
            data = self._get("channels", part=part, id=items[0]["snippet"]["channelId"])
        items = data.get("items") or []
        return self._channel(items[0]) if items else None

    @staticmethod
    def _channel(it: dict) -> dict:
        sn, st = it.get("snippet", {}), it.get("statistics", {})
        return {
            "channel_id": it["id"],
            "handle": sn.get("customUrl") if (sn.get("customUrl") or "").startswith("@") else None,
            "title": sn.get("title"),
            "description": sn.get("description"),
            "subscriber_count": None if st.get("hiddenSubscriberCount") else _int(st.get("subscriberCount")),
            "video_count": _int(st.get("videoCount")),
            "view_count": _int(st.get("viewCount")),
            "uploads_playlist_id": it.get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads"),
            "thumbnail_url": _thumb(sn),
            "meta_source": "api",
        }

    def iter_upload_ids(self, uploads_playlist_id: str, limit: int | None = None) -> Iterator[str]:
        token, n = None, 0
        while True:
            data = self._get("playlistItems", part="contentDetails", playlistId=uploads_playlist_id,
                             maxResults=50, pageToken=token)
            for it in data.get("items", []):
                yield it["contentDetails"]["videoId"]
                n += 1
                if limit and n >= limit:
                    return
            token = data.get("nextPageToken")
            if not token:
                return

    # -------------------------------------------------------------- videos
    def videos(self, ids: list[str]) -> list[dict]:
        out = []
        for i in range(0, len(ids), 50):
            data = self._get("videos", part="snippet,contentDetails,statistics,liveStreamingDetails",
                             id=",".join(ids[i:i + 50]), maxResults=50)
            out.extend(self._video(it) for it in data.get("items", []))
        return out

    @staticmethod
    def _video(it: dict) -> dict:
        sn, cd, st = it.get("snippet", {}), it.get("contentDetails", {}), it.get("statistics", {})
        dur = parse_duration(cd.get("duration"))
        title, desc = sn.get("title") or "", sn.get("description") or ""
        return {
            "video_id": it["id"],
            "channel_id": sn.get("channelId"),
            "channel_title": sn.get("channelTitle"),
            "title": title,
            "description": desc,
            "published_at": sn.get("publishedAt"),
            "published_approx": 0,
            "duration_s": dur,
            "view_count": _int(st.get("viewCount")),
            "like_count": _int(st.get("likeCount")),
            "comment_count": _int(st.get("commentCount")),
            "kind": classify_kind(dur, title, desc, sn.get("liveBroadcastContent"),
                                  "liveStreamingDetails" in it),
            "tags": sn.get("tags") or [],
            "category_id": sn.get("categoryId"),
            "language": sn.get("defaultAudioLanguage") or sn.get("defaultLanguage"),
            "has_captions": 1 if cd.get("caption") == "true" else 0 if cd.get("caption") == "false" else None,
            "thumbnail_url": _thumb(sn),
            "meta_source": "api",
        }

    def channel_catalogue(self, ref: str, limit: int | None = None) -> tuple[dict, list[dict]]:
        ch = self.resolve_channel(ref)
        if not ch:
            raise ApiError(f"channel not found: {ref}")
        ids = list(self.iter_upload_ids(ch["uploads_playlist_id"], limit=limit)) if ch["uploads_playlist_id"] else []
        return ch, self.videos(ids)


    def probe(self, ref: str = "@YouTube") -> dict:
        """One channels.list call (1 quota unit) that proves the key, the
        enabled API and the key restrictions all work end to end."""
        ch = self.resolve_channel(ref)
        if not ch:
            raise ApiError(f"key works, but channel {ref} was not found", status=200)
        return ch


def mask(key: str | None) -> str:
    if not key:
        return "(not set)"
    return f"{key[:4]}…{key[-4:]} ({len(key)} chars)" if len(key) > 10 else "(too short to be a key)"


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
