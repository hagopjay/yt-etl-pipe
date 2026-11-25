"""Runtime configuration, all from environment variables so the CLI, the
server and the worker agree without a config file.

  YTKB_HOME          data folder (default ./ytkb-data). Holds kb.sqlite3,
                     raw/ (lossless API / transcript payloads) and exports/.
  YT_API_KEY         YouTube Data API v3 key. Optional; without it channel
                     and video metadata come from yt-dlp.
  YTKB_LANGS         preferred transcript languages, comma-separated
                     (default "en,en-US,en-GB"). Falls back to whatever the
                     video has, preferring human-made over auto captions.
  YTKB_DELAY         seconds between transcript fetches (default 2.5, plus
                     0-1.5 s jitter). YouTube IP-blocks bursts even from
                     home connections, so the worker is deliberately polite.
  YTKB_PROXY_USER /  optional Webshare rotating-residential credentials.
  YTKB_PROXY_PASS    Not needed from a home IP; needed from any cloud IP.
  YTKB_PROXY_URL     optional generic http(s) proxy instead of Webshare.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    home: Path
    api_key: str | None
    langs: tuple[str, ...]
    delay: float
    proxy_user: str | None
    proxy_pass: str | None
    proxy_url: str | None

    @property
    def db_path(self) -> Path:
        return self.home / "kb.sqlite3"

    @property
    def raw_dir(self) -> Path:
        return self.home / "raw"

    @property
    def export_dir(self) -> Path:
        return self.home / "exports"


def load(home: str | os.PathLike | None = None) -> Settings:
    h = Path(home or os.environ.get("YTKB_HOME") or "ytkb-data").expanduser().resolve()
    langs = tuple(x.strip() for x in os.environ.get("YTKB_LANGS", "en,en-US,en-GB").split(",") if x.strip())
    return Settings(
        home=h,
        api_key=os.environ.get("YT_API_KEY") or None,
        langs=langs or ("en",),
        delay=float(os.environ.get("YTKB_DELAY", "2.5")),
        proxy_user=os.environ.get("YTKB_PROXY_USER") or None,
        proxy_pass=os.environ.get("YTKB_PROXY_PASS") or None,
        proxy_url=os.environ.get("YTKB_PROXY_URL") or None,
    )
