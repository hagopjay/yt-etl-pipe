"""
youtube_url.py — shared URL-parsing helper (bundled into whichever
Lambda zips need it; kept as one small module instead of copy-pasted so
the regex has exactly one place to fix if YouTube adds a new URL shape).
"""

import re

# Covers: youtube.com/watch?v=ID, youtu.be/ID, youtube.com/shorts/ID,
# youtube.com/embed/ID, youtube.com/v/ID, with or without extra query
# params, with or without a leading www./m., http or https.
_PATTERNS = [
    re.compile(r"(?:youtube\.com/watch\?(?:.*&)?v=)([A-Za-z0-9_-]{11})"),
    re.compile(r"(?:youtu\.be/)([A-Za-z0-9_-]{11})"),
    re.compile(r"(?:youtube\.com/shorts/)([A-Za-z0-9_-]{11})"),
    re.compile(r"(?:youtube\.com/embed/)([A-Za-z0-9_-]{11})"),
    re.compile(r"(?:youtube\.com/v/)([A-Za-z0-9_-]{11})"),
]

_BARE_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")


def extract_video_id(raw: str) -> str | None:
    """Returns an 11-char YouTube video ID from any common URL shape, or
    the input itself if it's already a bare ID. Returns None if nothing
    recognizable is found -- callers must treat that as a validation
    failure, not silently proceed with a garbage ID."""
    raw = raw.strip()

    if _BARE_ID.match(raw):
        return raw

    for pattern in _PATTERNS:
        m = pattern.search(raw)
        if m:
            return m.group(1)

    return None
