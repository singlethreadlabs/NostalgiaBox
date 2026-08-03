from __future__ import annotations

from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from nostalgiabox.server.analytics import AnalyticsStore, RETENTION_SECONDS
from nostalgiabox.server.database import Database
from nostalgiabox.server.schedule import Program


def program(kind: str = "show", show_name: str = "Rugrats") -> Program:
    return Program(
        id=10,
        channel_number=2,
        channel_name="Nick 2001",
        media_id=20,
        kind=kind,
        path=Path(f"/media/{show_name}/Episode One.mp4"),
        starts_at=0,
        ends_at=1800,
        delivery_mode="direct",
        show_name=show_name,
    )


@pytest.fixture
def store(tmp_path):
    database = Database(tmp_path / "server.db")
    database.initialize()
    return AnalyticsStore(database, "America/Chicago")


def test_database_migration_preserves_existing_data(tmp_path):
    database = Database(tmp_path / "server.db")
    database.initialize()
    with database.connect() as connection:
        connection.execute(
            """
            INSERT INTO media_items
                (path, kind, duration, size, mtime_ns, indexed_at)
            VALUES ('/media/show.mp4', 'show', 60, 1, 1, 1)
            """
        )

    database.initialize()

    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM media_items").fetchone()[0] == 1
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE name='viewing_sessions'"
        ).fetchone()


def test_activity_counts_only_playing_time_and_caps_gaps(store):
    store.start_session("one", program(), "browser", 100)
    assert store.activity("one", True, 101)
    assert store.activity("one", True, 111)
    assert store.activity("one", False, 116)
    assert store.activity("one", False, 120)
    assert store.activity("one", True, 121)
    assert store.finish_session("one", 200)

    summary = store.summary(0, 300)

    assert summary["total_watch_seconds"] == 45
    assert summary["session_count"] == 1
    assert summary["shows"][0]["show_name"] == "Rugrats"
    assert summary["shows"][0]["session_count"] == 1


def test_summary_filters_clients_and_non_show_content(store):
    for session_id, item, client in (
        ("browser", program(), "browser"),
        ("fire", program(show_name="Hey Arnold"), "fire_tv"),
        ("bumper", program(kind="bumper", show_name="bumper"), "browser"),
    ):
        store.start_session(session_id, item, client, 100)
        store.activity(session_id, True, 100)
        store.finish_session(session_id, 110)

    assert store.summary(0, 200)["total_watch_seconds"] == 20
    filtered = store.summary(0, 200, "fire_tv")
    assert filtered["total_watch_seconds"] == 10
    assert filtered["shows"][0]["show_name"] == "Hey Arnold"


def test_history_is_paginated_and_clips_range(store):
    for index in range(3):
        session_id = f"session-{index}"
        store.start_session(session_id, program(show_name=f"Show {index}"), "browser", 100 + index)
        store.activity(session_id, True, 100 + index)
        store.finish_session(session_id, 110 + index)

    first = store.history(105, 200, None, 2, None)
    assert len(first["items"]) == 2
    assert first["next_cursor"]
    timestamp, session_id = first["next_cursor"].split(":", 1)
    second = store.history(105, 200, None, 2, (float(timestamp), session_id))
    assert len(second["items"]) == 1
    assert sum(item["watch_seconds"] for item in first["items"] + second["items"]) == 18


def test_cleanup_expires_inactive_sessions_without_phantom_time_and_retains_one_year(store):
    store.start_session("expired", program(), "browser", 100)
    store.activity("expired", True, 100)
    store.cleanup(now=500, inactivity_seconds=60)
    assert store.summary(0, 1000)["total_watch_seconds"] == 0

    store.start_session("old", program(), "browser", 600)
    store.cleanup(now=600 + RETENTION_SECONDS + 1, inactivity_seconds=60)
    with store.database.connect() as connection:
        ids = {row[0] for row in connection.execute("SELECT id FROM viewing_sessions")}
    assert "old" not in ids


def test_concurrent_duplicate_heartbeat_does_not_double_count(store):
    store.start_session("concurrent", program(), "browser", 100)
    store.activity("concurrent", True, 100)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: store.activity("concurrent", True, 110), range(2)))
    store.finish_session("concurrent", 110)
    assert results == [True, True]
    assert store.summary(0, 200)["total_watch_seconds"] == 10


def test_daily_series_uses_configured_timezone_and_includes_empty_days(store):
    zone = ZoneInfo("America/Chicago")
    started = datetime(2026, 8, 3, 23, 59, 55, tzinfo=zone).timestamp()
    store.start_session("midnight", program(), "browser", started)
    store.activity("midnight", True, started)
    store.finish_session("midnight", started + 10)

    summary = store.summary(started - 86400, started + 86400)

    assert summary["daily"] == [
        {"date": "2026-08-02", "watch_seconds": 0.0},
        {"date": "2026-08-03", "watch_seconds": 5.0},
        {"date": "2026-08-04", "watch_seconds": 5.0},
    ]
