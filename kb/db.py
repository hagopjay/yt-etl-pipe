"""SQLite schema for the knowledge store.

One file, WAL mode, foreign keys on. The layout separates what is
*re-fetchable* from what is *yours*:

  channels / videos     metadata mirrored from YouTube (refreshable)
  transcripts           one row per video: status + language + full text
  segments              the caption snippets exactly as fetched (lossless)
  chunks + chunks_fts   ~60 s windows of segments: the unit of search and
                        of RAG retrieval, each with start/end seconds so
                        every hit deep-links to the moment in the video
  videos_fts            title + description search
  curation              YOUR board: lane, hand-ranked order, interest, notes
  jobs                  the work queue (metadata, transcripts, channel scrapes)

Why chunks and segments both: segments are 2-5 s and often split words
across lines (auto-captions), so they're a poor search unit and terrible
embedding input; chunks are a derived, rebuildable view. Keeping the
segments means chunking can be re-tuned later without refetching anything.

Why FTS5 external-content tables: the text lives once (in chunks / videos);
the FTS index stores only the inverted index, kept in sync by triggers.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS channels (
    channel_id          TEXT PRIMARY KEY,           -- UC...
    handle              TEXT,                       -- @name
    title               TEXT,
    description         TEXT,
    subscriber_count    INTEGER,
    video_count         INTEGER,
    view_count          INTEGER,
    uploads_playlist_id TEXT,                       -- UU... (Data API)
    thumbnail_url       TEXT,
    followed            INTEGER NOT NULL DEFAULT 0,
    interest            INTEGER,                    -- 0..5, yours
    notes               TEXT,                       -- yours
    meta_source         TEXT,                       -- api | yt-dlp | takeout | rss
    last_scraped_at     TEXT,
    created_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    updated_at          TEXT
);
CREATE INDEX IF NOT EXISTS channels_handle ON channels(handle);

CREATE TABLE IF NOT EXISTS videos (
    video_id        TEXT PRIMARY KEY,
    channel_id      TEXT REFERENCES channels(channel_id),
    title           TEXT,
    description     TEXT,
    published_at    TEXT,                           -- ISO 8601 UTC
    published_approx INTEGER NOT NULL DEFAULT 0,    -- 1 = from "3 years ago" text (yt-dlp flat)
    duration_s      INTEGER,
    view_count      INTEGER,
    like_count      INTEGER,
    comment_count   INTEGER,
    kind            TEXT,                           -- video | short | live | upcoming
    tags            TEXT,                           -- JSON array
    category_id     TEXT,
    language        TEXT,
    has_captions    INTEGER,                        -- Data API contentDetails.caption
    thumbnail_url   TEXT,
    meta_source     TEXT,
    meta_fetched_at TEXT,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    updated_at      TEXT
);
CREATE INDEX IF NOT EXISTS videos_channel_views ON videos(channel_id, view_count DESC);
CREATE INDEX IF NOT EXISTS videos_published ON videos(published_at);

CREATE TABLE IF NOT EXISTS curation (
    video_id   TEXT PRIMARY KEY REFERENCES videos(video_id) ON DELETE CASCADE,
    lane       TEXT NOT NULL DEFAULT 'inbox'
               CHECK (lane IN ('inbox','must_watch','watching','done','skip')),
    rank       REAL NOT NULL,                       -- fractional index: drag = midpoint
    priority   REAL NOT NULL DEFAULT 0,             -- suggested score (ingest/ channel popularity)
    interest   INTEGER,                             -- 0..5
    note       TEXT,
    sources    TEXT,                                -- JSON array: watch_later, channel:<id>, manual, ...
    tags       TEXT,                                -- JSON array: heuristic route tags
    added_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS curation_lane_rank ON curation(lane, rank);

CREATE TABLE IF NOT EXISTS transcripts (
    video_id      TEXT PRIMARY KEY REFERENCES videos(video_id) ON DELETE CASCADE,
    status        TEXT NOT NULL
                  CHECK (status IN ('ok','disabled','no_captions','unavailable','blocked','error')),
    reason        TEXT,                             -- finer cause: age_restricted, members_only, ...
    language      TEXT,
    is_generated  INTEGER,                          -- 1 = YouTube auto-captions
    source        TEXT,                             -- caption_manual | caption_auto
    segment_count INTEGER,
    word_count    INTEGER,
    full_text     TEXT,
    error         TEXT,
    fetched_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
CREATE INDEX IF NOT EXISTS transcripts_status ON transcripts(status);

CREATE TABLE IF NOT EXISTS segments (
    video_id TEXT NOT NULL REFERENCES transcripts(video_id) ON DELETE CASCADE,
    idx      INTEGER NOT NULL,
    start    REAL NOT NULL,
    duration REAL NOT NULL,
    text     TEXT NOT NULL,
    PRIMARY KEY (video_id, idx)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS chunks (
    chunk_id INTEGER PRIMARY KEY,
    video_id TEXT NOT NULL REFERENCES transcripts(video_id) ON DELETE CASCADE,
    idx      INTEGER NOT NULL,
    start    REAL NOT NULL,
    end      REAL NOT NULL,
    text     TEXT NOT NULL,
    UNIQUE (video_id, idx)
);

CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    text, content='chunks', content_rowid='chunk_id',
    tokenize='porter unicode61 remove_diacritics 2'
);
CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
    INSERT INTO chunks_fts(rowid, text) VALUES (new.chunk_id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, text) VALUES ('delete', old.chunk_id, old.text);
END;
CREATE TRIGGER IF NOT EXISTS chunks_au AFTER UPDATE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, text) VALUES ('delete', old.chunk_id, old.text);
    INSERT INTO chunks_fts(rowid, text) VALUES (new.chunk_id, new.text);
END;

CREATE VIRTUAL TABLE IF NOT EXISTS videos_fts USING fts5(
    title, description, content='videos', content_rowid='rowid',
    tokenize='porter unicode61 remove_diacritics 2'
);
CREATE TRIGGER IF NOT EXISTS videos_ai AFTER INSERT ON videos BEGIN
    INSERT INTO videos_fts(rowid, title, description) VALUES (new.rowid, new.title, new.description);
END;
CREATE TRIGGER IF NOT EXISTS videos_ad AFTER DELETE ON videos BEGIN
    INSERT INTO videos_fts(videos_fts, rowid, title, description)
    VALUES ('delete', old.rowid, old.title, old.description);
END;
CREATE TRIGGER IF NOT EXISTS videos_au AFTER UPDATE OF title, description ON videos BEGIN
    INSERT INTO videos_fts(videos_fts, rowid, title, description)
    VALUES ('delete', old.rowid, old.title, old.description);
    INSERT INTO videos_fts(rowid, title, description) VALUES (new.rowid, new.title, new.description);
END;

CREATE TABLE IF NOT EXISTS jobs (
    id         INTEGER PRIMARY KEY,
    kind       TEXT NOT NULL CHECK (kind IN ('meta','transcript','scrape_channel')),
    target     TEXT NOT NULL,                       -- video_id or channel ref
    args       TEXT,                                -- JSON
    status     TEXT NOT NULL DEFAULT 'queued'
               CHECK (status IN ('queued','running','done','failed')),
    priority   INTEGER NOT NULL DEFAULT 0,          -- higher runs first
    attempts   INTEGER NOT NULL DEFAULT 0,
    not_before TEXT,                                -- backoff: don't run before this UTC time
    error      TEXT,
    result     TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    updated_at TEXT
);
-- At most one pending job per (kind, target): enqueueing twice is a no-op.
CREATE UNIQUE INDEX IF NOT EXISTS jobs_pending_unique
    ON jobs(kind, target) WHERE status IN ('queued','running');
CREATE INDEX IF NOT EXISTS jobs_pick ON jobs(status, priority DESC, id);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))


def open_store(path: str | Path) -> sqlite3.Connection:
    conn = connect(path)
    init(conn)
    return conn


class tx:
    """`with tx(conn): ...` -- explicit BEGIN IMMEDIATE / COMMIT. The
    connection runs in autocommit mode (isolation_level=None) so every
    multi-statement write must opt into a transaction deliberately."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def __enter__(self):
        self.conn.execute("BEGIN IMMEDIATE")
        return self.conn

    def __exit__(self, exc_type, exc, tb):
        self.conn.execute("ROLLBACK" if exc_type else "COMMIT")
        return False
