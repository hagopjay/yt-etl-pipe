"""yt-dlp backend: no API key. Lists a channel's tabs in *flat* mode (one
page-walk per tab, no per-video requests), which still yields id, title,
duration, view count, an approximate upload date and live/shorts status --
everything needed to rank a channel's catalogue.

Trade-offs vs the Data API: unofficial (breaks when YouTube changes and
needs `pip install -U yt-dlp`), no like/comment counts or exact dates in
flat mode, but it knows exactly which uploads are Shorts (separate tab)
and needs no Google Cloud project.
"""

from __future__ import annotations

from datetime import datetime, timezone

TABS = ("videos", "streams", "shorts")
_KIND = {"videos": "video", "streams": "live", "shorts": "short"}


class _Silent:
    """yt-dlp prints ERROR lines to stderr even when quiet; the worker
    already records every failure on its job row."""
    def debug(self, msg): pass
    def info(self, msg): pass
    def warning(self, msg): pass
    def error(self, msg): pass


def _ydl(**extra):
    import yt_dlp  # imported lazily: optional dependency
    opts = {
        "quiet": True, "no_warnings": True, "skip_download": True, "logger": _Silent(),
        "ignore_no_formats_error": True,   # metadata only; formats may need a JS runtime
        "extractor_args": {"youtubetab": {"approximate_date": ["true"]}},
    }
    opts.update(extra)
    return yt_dlp.YoutubeDL(opts)


def _base_url(ref: str) -> str:
    if ref.startswith("UC"):
        return f"https://www.youtube.com/channel/{ref}"
    if ref.startswith("@"):
        return f"https://www.youtube.com/{ref}"
    return ref.rstrip("/")


def _ts_iso(ts) -> str | None:
    if not ts:
        return None
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _thumb(entry: dict) -> str | None:
    ths = entry.get("thumbnails") or []
    return ths[-1].get("url") if ths else entry.get("thumbnail")


def channel_catalogue(ref: str, tabs=("videos", "streams"), limit: int | None = None,
                      extract=None) -> tuple[dict, list[dict]]:
    """`extract(url) -> info dict` is injectable for tests."""
    if extract is None:
        ydl = _ydl(extract_flat="in_playlist", **({"playlistend": limit} if limit else {}))
        extract = lambda url: ydl.extract_info(url, download=False)  # noqa: E731
    base = _base_url(ref)
    channel, videos, errors = None, {}, []
    for tab in tabs:
        try:
            info = extract(f"{base}/{tab}")
        except Exception as e:  # a channel with no Shorts/streams has no such tab
            errors.append(f"{tab}: {str(e).splitlines()[0][:200]}")
            continue
        if not info:
            continue
        if channel is None:
            handle = info.get("uploader_id") if str(info.get("uploader_id") or "").startswith("@") else None
            channel = {
                "channel_id": info.get("channel_id"),
                "handle": handle,
                "title": info.get("channel") or info.get("uploader") or info.get("title"),
                "description": info.get("description"),
                "subscriber_count": info.get("channel_follower_count"),
                "video_count": None, "view_count": None, "uploads_playlist_id": None,
                "thumbnail_url": _thumb(info),
                "meta_source": "yt-dlp",
            }
        for e in info.get("entries") or []:
            vid = e.get("id")
            if not vid or vid in videos or len(vid) != 11:
                continue
            ls = e.get("live_status")
            kind = "upcoming" if ls == "is_upcoming" else "live" if ls in ("is_live", "was_live") else _KIND[tab]
            if "/shorts/" in (e.get("url") or ""):
                kind = "short"
            videos[vid] = {
                "video_id": vid, "channel_id": e.get("channel_id") or (channel or {}).get("channel_id"),
                "title": e.get("title"), "description": e.get("description"),
                "published_at": _ts_iso(e.get("timestamp") or e.get("release_timestamp")),
                "published_approx": 1 if e.get("timestamp") else 0,
                "duration_s": int(e["duration"]) if e.get("duration") is not None else None,
                "view_count": e.get("view_count"),
                "like_count": None, "comment_count": None, "kind": kind, "tags": [],
                "category_id": None, "language": None, "has_captions": None,
                "thumbnail_url": f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg",
                "meta_source": "yt-dlp",
            }
    if channel is None or not channel.get("channel_id"):
        raise RuntimeError(f"could not resolve channel {ref}: {'; '.join(errors) or 'no data'}")
    if channel["video_count"] is None:
        channel["video_count"] = len(videos)
    return channel, list(videos.values())


def video(video_id: str, extract=None) -> dict | None:
    """Full (non-flat) extraction for one video: exact date, likes, tags,
    captions flag. ~1-2 s per video."""
    if extract is None:
        ydl = _ydl()
        extract = lambda url: ydl.extract_info(url, download=False)  # noqa: E731
    info = extract(f"https://www.youtube.com/watch?v={video_id}")
    if not info:
        return None
    ud = info.get("upload_date")
    published = (info.get("timestamp") and _ts_iso(info["timestamp"])) or \
        (f"{ud[:4]}-{ud[4:6]}-{ud[6:8]}T00:00:00Z" if ud and len(ud) == 8 else None)
    ls = info.get("live_status")
    dur = info.get("duration")
    kind = "upcoming" if ls == "is_upcoming" else "live" if ls in ("is_live", "was_live", "post_live") else \
        "short" if (dur is not None and dur <= 180 and "/shorts/" in (info.get("webpage_url") or "")) or \
        (dur is not None and dur <= 60) else "video"
    return {
        "video_id": video_id, "channel_id": info.get("channel_id"),
        "channel_title": info.get("channel"), "channel_handle":
            info.get("uploader_id") if str(info.get("uploader_id") or "").startswith("@") else None,
        "title": info.get("title"), "description": info.get("description"),
        "published_at": published, "published_approx": 0,
        "duration_s": int(dur) if dur is not None else None,
        "view_count": info.get("view_count"), "like_count": info.get("like_count"),
        "comment_count": info.get("comment_count"), "kind": kind, "tags": info.get("tags") or [],
        "category_id": (info.get("categories") or [None])[0], "language": info.get("language"),
        "has_captions": 1 if (info.get("subtitles") or info.get("automatic_captions")) else None,
        "thumbnail_url": f"https://i.ytimg.com/vi/{video_id}/mqdefault.jpg",
        "meta_source": "yt-dlp",
    }
