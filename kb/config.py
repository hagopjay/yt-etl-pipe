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

Any of these can also live in a `.env` file (KEY=value per line) in the
current directory or in YTKB_HOME. Real environment variables win over
.env; .env in the cwd wins over .env in YTKB_HOME. Both locations are
git-ignored -- never commit the key.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def read_dotenv(path: Path) -> dict[str, str]:
    """Minimal .env parser: KEY=value, optional `export `, optional quotes,
    `#` comments. No interpolation -- an API key needs none."""
    out: dict[str, str] = {}
    try:
        lines = Path(path).read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return out
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.removeprefix("export ").split("=", 1)
        k, v = k.strip(), v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        elif " #" in v:
            v = v.split(" #", 1)[0].rstrip()
        if k:
            out[k] = v
    return out


def _apply_dotenv(path: Path, sources: dict[str, str]) -> None:
    for k, v in read_dotenv(path).items():
        if k not in os.environ:
            os.environ[k] = v
            sources[k] = str(path)


# Where each setting came from (env var name -> .env path); "environment" if
# it was already set. Lets `kb check` say exactly which key is in use.
SOURCES: dict[str, str] = {}


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
    if "YT_API_KEY" in os.environ and "YT_API_KEY" not in SOURCES:
        SOURCES["YT_API_KEY"] = "environment"
    _apply_dotenv(Path.cwd() / ".env", SOURCES)
    h = Path(home or os.environ.get("YTKB_HOME") or "ytkb-data").expanduser().resolve()
    _apply_dotenv(h / ".env", SOURCES)
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
