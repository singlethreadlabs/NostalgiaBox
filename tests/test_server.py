from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from nostalgiabox.config import Config, config_from_dict
from nostalgiabox.server.app import create_app
from nostalgiabox.server.database import Database
from nostalgiabox.server.media import MediaIndexer, MediaProbe
from nostalgiabox.server.playback import PlaybackManager
from nostalgiabox.server.schedule import Scheduler
from nostalgiabox.server.settings import ServerSettings


def _make_media(root: Path, relative: str, count: int = 1) -> Path:
    folder = root / relative
    folder.mkdir(parents=True, exist_ok=True)
    for index in range(count):
        (folder / f"item-{index}.mp4").write_bytes(b"0123456789")
    return folder


def _config(media_root: Path):
    _make_media(media_root, "tv/rugrats", 2)
    _make_media(media_root, "tv/arnold", 2)
    _make_media(media_root, "bumpers", 2)
    _make_media(media_root, "commercials", 3)
    return config_from_dict(
        {
            "schedule": {"timezone": "America/Chicago", "horizon_hours": 1},
            "start_channel": 2,
            "channels": [
                {
                    "number": 2,
                    "name": "Nick 2001",
                    "shows": [
                        str(media_root / "tv/rugrats"),
                        str(media_root / "tv/arnold"),
                    ],
                    "bumpers": [str(media_root / "bumpers")],
                    "commercials": [str(media_root / "commercials")],
                }
            ],
        }
    )


@pytest.fixture
def fixed_probe(monkeypatch):
    probe = MediaProbe(
        duration=600.0,
        container="mov,mp4,m4a,3gp,3g2,mj2",
        video_codec="h264",
        audio_codec="aac",
    )
    monkeypatch.setattr("nostalgiabox.server.media.probe_media", lambda path: probe)
    return probe


def test_media_index_and_persistent_schedule(tmp_path, fixed_probe):
    media_root = tmp_path / "media"
    config = _config(media_root)
    database = Database(tmp_path / "data" / "server.db")
    database.initialize()
    indexer = MediaIndexer(database, config, media_root)
    assert indexer.refresh() == 9

    scheduler = Scheduler(database, config)
    at = 1_800_000_000.0
    scheduler.fill(at)
    first = scheduler.now(2, at)
    restarted = Scheduler(database, config).now(2, at)
    assert restarted.id == first.id
    assert restarted.path == first.path
    assert restarted.starts_at <= at < restarted.ends_at

    with database.connect() as connection:
        kinds = [
            row["kind"]
            for row in connection.execute(
                """
                SELECT kind FROM programs
                WHERE channel_number=2 ORDER BY starts_at LIMIT 5
                """
            )
        ]
    assert kinds[0:3] == ["show", "bumper", "commercial"]
    assert kinds[3] in {"commercial", "show"}


def test_media_index_accepts_a_direct_bumper_file(tmp_path, fixed_probe):
    media_root = tmp_path / "media"
    show = _make_media(media_root, "shows/example")
    bumper = _make_media(media_root, "bumpers") / "item-0.mp4"
    config = config_from_dict(
        {
            "channels": [
                {
                    "number": 2,
                    "name": "Example",
                    "shows": [str(show)],
                    "bumpers": [str(bumper)],
                }
            ]
        }
    )
    database = Database(tmp_path / "server.db")
    database.initialize()

    assert MediaIndexer(database, config, media_root).refresh() == 2
    with database.connect() as connection:
        indexed_bumper = connection.execute(
            """
            SELECT m.path FROM channel_media cm
            JOIN media_items m ON m.id=cm.media_id
            WHERE cm.channel_number=2 AND cm.kind='bumper'
            """
        ).fetchone()
    assert Path(indexed_bumper["path"]) == bumper


def test_show_rotation_balances_uneven_folders(tmp_path, fixed_probe):
    media_root = tmp_path / "media"
    large_show = _make_media(media_root, "shows/large", 8)
    small_show = _make_media(media_root, "shows/small", 1)
    config = config_from_dict(
        {
            "schedule": {"horizon_hours": 2},
            "channels": [
                {
                    "number": 2,
                    "name": "Balanced",
                    "shows": [str(large_show), str(small_show)],
                }
            ],
        }
    )
    database = Database(tmp_path / "server.db")
    database.initialize()
    MediaIndexer(database, config, media_root).refresh()
    Scheduler(database, config).fill(1_800_000_000.0)

    with database.connect() as connection:
        paths = [
            Path(row["path"])
            for row in connection.execute(
                """
                SELECT m.path FROM programs p
                JOIN media_items m ON m.id=p.media_id
                WHERE p.channel_number=2 AND p.kind='show'
                ORDER BY p.starts_at LIMIT 10
                """
            )
        ]
    assert len(paths) >= 6
    assert all(left.parent != right.parent for left, right in zip(paths, paths[1:]))
    first_six = [path.parent for path in paths[:6]]
    assert first_six.count(large_show) == first_six.count(small_show) == 3


def test_database_migrates_existing_channel_media_table(tmp_path):
    database = Database(tmp_path / "legacy.db")
    with database.connect() as connection:
        connection.execute(
            """
            CREATE TABLE channel_media (
                channel_number INTEGER NOT NULL,
                media_id INTEGER NOT NULL,
                kind TEXT NOT NULL,
                PRIMARY KEY(channel_number, media_id, kind)
            )
            """
        )
    database.initialize()
    with database.connect() as connection:
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(channel_media)")
        }
    assert "pool_key" in columns


def test_database_creates_scheduler_indexes(tmp_path):
    database = Database(tmp_path / "server.db")
    database.initialize()
    with database.connect() as connection:
        indexes = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            )
        }
    assert {
        "channel_media_pool",
        "programs_current",
        "programs_latest_end",
        "programs_latest_kind",
    } <= indexes


def test_media_path_must_stay_below_root(tmp_path, fixed_probe):
    media_root = tmp_path / "media"
    outside = _make_media(tmp_path, "outside")
    config = config_from_dict({"channels": [{"shows": [str(outside)]}]})
    database = Database(tmp_path / "server.db")
    database.initialize()
    with pytest.raises(ValueError, match="escapes"):
        MediaIndexer(database, config, media_root).refresh()


def test_refresh_retains_stale_media_referenced_by_schedule_history(
    tmp_path, fixed_probe
):
    media_root = tmp_path / "media"
    config = _config(media_root)
    database = Database(tmp_path / "server.db")
    database.initialize()
    indexer = MediaIndexer(database, config, media_root)
    indexer.refresh()

    stale_path = media_root / "tv/rugrats/item-0.mp4"
    with database.connect() as connection:
        media_id = connection.execute(
            "SELECT id FROM media_items WHERE path=?", (str(stale_path),)
        ).fetchone()["id"]
        connection.execute(
            """
            INSERT INTO programs
                (channel_number, media_id, kind, starts_at, ends_at)
            VALUES (2, ?, 'show', 1, 2)
            """,
            (media_id,),
        )
    stale_path.unlink()

    assert indexer.refresh() == 8
    with database.connect() as connection:
        assert connection.execute(
            "SELECT 1 FROM media_items WHERE id=?", (media_id,)
        ).fetchone()


def test_delivery_classification():
    assert MediaProbe(10, "mov,mp4", "h264", "aac").delivery_mode == "direct"
    assert MediaProbe(10, "matroska,webm", "h264", "aac").delivery_mode == "remux"
    assert MediaProbe(10, "avi", "mpeg4", "mp3").delivery_mode == "transcode"


def test_direct_playback_session_cleanup(tmp_path):
    from nostalgiabox.server.schedule import Program

    media = tmp_path / "video.mp4"
    media.write_bytes(b"video")
    program = Program(
        id=1,
        channel_number=2,
        channel_name="Nick",
        media_id=1,
        kind="show",
        path=media,
        starts_at=90,
        ends_at=200,
        delivery_mode="direct",
    )
    manager = PlaybackManager(tmp_path / "cache", ttl_seconds=30)
    session = manager.create(program, now=100)
    assert session.offset == 10
    session.expires_at = time.time() - 1
    manager.cleanup()
    with pytest.raises(KeyError):
        manager.get(session.id)
    assert not session.directory.exists()


def test_playback_manager_removes_only_abandoned_session_directories(tmp_path):
    cache = tmp_path / "cache"
    abandoned = cache / ("a" * 32)
    preserved_directory = cache / "thumbnails"
    cache.mkdir()
    abandoned.mkdir()
    preserved_directory.mkdir()
    preserved_file = cache / "README"
    preserved_file.write_text("keep")

    PlaybackManager(cache)

    assert not abandoned.exists()
    assert preserved_directory.exists()
    assert preserved_file.exists()


def test_channels_fill_once_and_return_all_current_programs(
    tmp_path, fixed_probe, monkeypatch
):
    media_root = tmp_path / "media"
    channels = []
    for number in range(2, 16):
        show = _make_media(media_root, f"shows/{number}")
        channels.append({"number": number, "name": f"Channel {number}", "shows": [str(show)]})
    config = config_from_dict(
        {"schedule": {"horizon_hours": 1}, "channels": channels}
    )
    database = Database(tmp_path / "server.db")
    database.initialize()
    MediaIndexer(database, config, media_root).refresh()
    scheduler = Scheduler(database, config)
    calls = 0
    original_fill = scheduler.fill

    def counted_fill(at=None):
        nonlocal calls
        calls += 1
        return original_fill(at)

    monkeypatch.setattr(scheduler, "fill", counted_fill)
    lineup = scheduler.channels(1_800_000_000.0)

    assert calls == 1
    assert [number for number, _, _ in lineup] == list(range(2, 16))


def test_channels_handles_empty_config(tmp_path):
    scheduler = Scheduler(Database(tmp_path / "server.db"), Config(channels=[]))
    assert scheduler.channels(1_800_000_000.0) == []


def test_api_health_channels_playback_and_ranges(
    tmp_path, fixed_probe, monkeypatch
):
    media_root = tmp_path / "media"
    config = _config(media_root)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"""
schedule:
  timezone: America/Chicago
  horizon_hours: 1
start_channel: 2
channels:
  - number: 2
    name: Nick 2001
    shows:
      - {media_root / "tv/rugrats"}
      - {media_root / "tv/arnold"}
    bumpers:
      - {media_root / "bumpers"}
    commercials:
      - {media_root / "commercials"}
"""
    )
    settings = ServerSettings(
        config_path=config_path,
        database_path=tmp_path / "data" / "server.db",
        cache_dir=tmp_path / "cache",
        media_root=media_root,
        session_ttl_seconds=60,
    )
    with TestClient(create_app(settings)) as client:
        health = client.get("/api/v1/health")
        assert health.status_code == 200
        assert health.json()["indexed_media"] == 9
        assert health.json()["active_sessions"] == 0
        assert health.json()["active_ffmpeg_processes"] == 0

        channels = client.get("/api/v1/channels").json()
        assert channels["start_channel"] == 2
        assert channels["channels"][0]["channel"]["name"] == "Nick 2001"

        now = client.get("/api/v1/channels/2/now")
        assert now.status_code == 200
        assert now.json()["program"]["elapsed_seconds"] >= 0
        assert client.get("/api/v1/channels/99/now").status_code == 404

        playback = client.post(
            "/api/v1/playback-sessions", json={"channel_number": 2}
        )
        assert playback.status_code == 201
        descriptor = playback.json()
        assert descriptor["delivery_mode"] == "direct"
        assert descriptor["hls_url"] is None
        assert descriptor["browser_url"].endswith("/stream.mp4")

        ranged = client.get(
            descriptor["media_url"], headers={"range": "bytes=0-3"}
        )
        assert ranged.status_code == 206
        assert ranged.content == b"0123"

        deleted = client.delete(
            f"/api/v1/playback-sessions/{descriptor['id']}"
        )
        assert deleted.status_code == 204
        assert client.post("/api/v1/admin/refresh").status_code == 200


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg unavailable")
def test_real_ffmpeg_remux_and_transcode_hls(tmp_path):
    from nostalgiabox.server.media import probe_media
    from nostalgiabox.server.schedule import Program

    sources = [
        ("remux.mkv", ["-c:v", "libx264", "-c:a", "aac"], "remux"),
        ("transcode.avi", ["-c:v", "mpeg4", "-c:a", "libmp3lame"], "transcode"),
    ]
    manager = PlaybackManager(tmp_path / "cache", ttl_seconds=60)
    try:
        for index, (filename, codecs, expected_mode) in enumerate(sources, start=1):
            path = tmp_path / filename
            subprocess.run(
                [
                    "ffmpeg",
                    "-nostdin",
                    "-loglevel",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "testsrc2=size=160x120:rate=24:duration=2",
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=440:duration=2",
                    *codecs,
                    "-shortest",
                    str(path),
                ],
                check=True,
            )
            probe = probe_media(path)
            assert probe.delivery_mode == expected_mode
            program = Program(
                id=index,
                channel_number=2,
                channel_name="Test",
                media_id=index,
                kind="show",
                path=path,
                starts_at=time.time() - 0.25,
                ends_at=time.time() + 1.75,
                delivery_mode=expected_mode,
            )
            session = manager.create(program)
            assert manager.active_session_count == 1
            manager.ensure_hls(session.id)
            assert manager.active_process_count == 1
            playlist = session.directory / "index.m3u8"
            deadline = time.monotonic() + 10
            while not playlist.is_file() and time.monotonic() < deadline:
                time.sleep(0.05)
            assert playlist.is_file()
            assert "#EXTM3U" in playlist.read_text()
            assert manager.release(session.id)
            assert manager.active_session_count == 0
            assert manager.active_process_count == 0

            browser_session = manager.create(program)
            stream = manager.fragmented_mp4(browser_session.id)
            first_chunk = next(stream)
            assert b"ftyp" in first_chunk
            stream.close()
            assert manager.release(browser_session.id)
    finally:
        manager.close()
