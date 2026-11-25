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
    pass


class QuotaExceeded(ApiError):
    pass


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
            if e.code == 403 and ("quotaExceeded" in body or "dailyLimitExceeded" in body):
                raise QuotaExceeded(body[:300]) from e
            raise ApiError(f"{endpoint} HTTP {e.code}: {body[:300]}") from e

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


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
