"""Small SQLite persistence layer shared by the media index and scheduler."""

from __future__ import annotations

import sqlite3
from pathlib import Path


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS media_items (
    id INTEGER PRIMARY KEY,
    path TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL CHECK(kind IN ('show', 'bumper', 'commercial')),
    duration REAL NOT NULL CHECK(duration > 0),
    container TEXT,
    video_codec TEXT,
    audio_codec TEXT,
    size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    indexed_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS channel_media (
    channel_number INTEGER NOT NULL,
    media_id INTEGER NOT NULL REFERENCES media_items(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK(kind IN ('show', 'bumper', 'commercial')),
    pool_key TEXT NOT NULL,
    PRIMARY KEY(channel_number, media_id, kind)
);
CREATE INDEX IF NOT EXISTS channel_media_pool
    ON channel_media(channel_number, kind, media_id);

CREATE TABLE IF NOT EXISTS programs (
    id INTEGER PRIMARY KEY,
    channel_number INTEGER NOT NULL,
    media_id INTEGER NOT NULL REFERENCES media_items(id),
    kind TEXT NOT NULL,
    starts_at REAL NOT NULL,
    ends_at REAL NOT NULL,
    UNIQUE(channel_number, starts_at)
);
CREATE INDEX IF NOT EXISTS programs_current
    ON programs(channel_number, starts_at, ends_at);
CREATE INDEX IF NOT EXISTS programs_latest_end
    ON programs(channel_number, ends_at DESC);
CREATE INDEX IF NOT EXISTS programs_latest_kind
    ON programs(channel_number, kind, starts_at DESC);

CREATE TABLE IF NOT EXISTS viewing_sessions (
    id TEXT PRIMARY KEY,
    program_id INTEGER,
    media_id INTEGER,
    channel_number INTEGER NOT NULL,
    channel_name TEXT NOT NULL,
    show_name TEXT NOT NULL,
    episode_title TEXT NOT NULL,
    media_kind TEXT NOT NULL CHECK(media_kind IN ('show', 'bumper', 'commercial')),
    client_type TEXT NOT NULL CHECK(client_type IN ('browser', 'fire_tv')),
    started_at REAL NOT NULL,
    ended_at REAL,
    is_playing INTEGER NOT NULL DEFAULT 0 CHECK(is_playing IN (0, 1)),
    active_since REAL,
    last_activity_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS viewing_sessions_started
    ON viewing_sessions(started_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS viewing_sessions_kind_client
    ON viewing_sessions(media_kind, client_type, started_at);

CREATE TABLE IF NOT EXISTS viewing_intervals (
    id INTEGER PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES viewing_sessions(id) ON DELETE CASCADE,
    started_at REAL NOT NULL,
    ended_at REAL NOT NULL,
    watch_seconds REAL NOT NULL CHECK(watch_seconds >= 0)
);
CREATE INDEX IF NOT EXISTS viewing_intervals_range
    ON viewing_intervals(started_at, ended_at);
CREATE INDEX IF NOT EXISTS viewing_intervals_session
    ON viewing_intervals(session_id);
"""


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def initialize(self) -> None:
        with self.connect() as connection:
            connection.executescript(SCHEMA)
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(channel_media)")
            }
            if "pool_key" not in columns:
                connection.execute(
                    "ALTER TABLE channel_media "
                    "ADD COLUMN pool_key TEXT NOT NULL DEFAULT ''"
                )
