"""Selection history must outlive a scheduler instance and its lookahead."""

from pathlib import Path

from nostalgiabox.config import config_from_dict
from nostalgiabox.server.database import Database
from nostalgiabox.server.media import MediaIndexer, MediaProbe
from nostalgiabox.server.schedule import Scheduler


START = 1_800_000_000.0


def _station(tmp_path, monkeypatch, shows, *, duration=600.0, ordered=()):
    media_root = tmp_path / "media"
    folders = []
    for name, episodes in shows.items():
        folder = media_root / name
        folder.mkdir(parents=True)
        for episode in episodes:
            (folder / episode).write_bytes(b"media")
        folders.append(folder)
    config = config_from_dict(
        {
            "schedule": {"horizon_hours": 24},
            "channels": [
                {
                    "number": 2,
                    "name": "Test station",
                    "shows": [str(folder) for folder in folders],
                    "ordered_shows": [str(media_root / name) for name in ordered],
                }
            ],
        }
    )
    monkeypatch.setattr("nostalgiabox.server.media.time.time", lambda: START)
    probe = MediaProbe(duration, "mov,mp4,m4a,3gp,3g2,mj2", "h264", "aac")
    monkeypatch.setattr("nostalgiabox.server.media.probe_media", lambda path: probe)
    database = Database(tmp_path / "server.db")
    database.initialize()
    indexer = MediaIndexer(database, config, media_root)
    indexer.refresh()
    return database, config, indexer


def _programs(database):
    with database.connect() as connection:
        return list(
            connection.execute(
                """
                SELECT m.path, p.starts_at, p.ends_at FROM programs p
                JOIN media_items m ON m.id=p.media_id
                WHERE p.channel_number=2 AND p.kind='show'
                ORDER BY p.starts_at
                """
            )
        )


def test_each_show_exhausts_episodes_before_repeating(tmp_path, monkeypatch):
    episodes = [f"S01E{episode:02}.mp4" for episode in range(1, 5)]
    database, config, _ = _station(
        tmp_path, monkeypatch, {"Arthur": episodes, "Rugrats": episodes}
    )
    Scheduler(database, config).fill(START)

    for show in ("Arthur", "Rugrats"):
        paths = [
            Path(row["path"])
            for row in _programs(database)
            if Path(row["path"]).parent.name == show
        ]
        assert len(paths) >= 8
        assert all(left != right for left, right in zip(paths, paths[1:]))
        for offset in range(0, len(paths) - 3, 4):
            assert len(set(paths[offset : offset + 4])) == 4


def test_episode_history_survives_refresh_and_scheduler_restart(tmp_path, monkeypatch):
    episodes = [f"S01E{episode:02}.mp4" for episode in range(1, 7)]
    database, config, indexer = _station(
        tmp_path, monkeypatch, {"Arthur": episodes}
    )
    Scheduler(database, config).fill(START)
    before = _programs(database)
    now = before[1]["starts_at"] + 1
    aired = [row["path"] for row in before[:2]]
    monkeypatch.setattr("nostalgiabox.server.media.time.time", lambda: now)

    indexer.refresh()
    Scheduler(database, config).fill(now)

    paths = [row["path"] for row in _programs(database)]
    assert paths[:2] == aired
    assert len(set(paths[:6])) == 6


def test_show_rotation_survives_incremental_fills(tmp_path, monkeypatch):
    database, config, _ = _station(
        tmp_path,
        monkeypatch,
        {"Arthur": ["one.mp4"], "Rugrats": ["one.mp4"], "Doug": ["one.mp4"]},
        duration=7200.0,
    )
    Scheduler(database, config).fill(START)
    for _ in range(24):
        last_end = _programs(database)[-1]["ends_at"]
        Scheduler(database, config).fill(last_end - 24 * 3600 + 1)

    shows = [Path(row["path"]).parent.name for row in _programs(database)]
    for offset in range(0, len(shows) - 2, 3):
        assert len(set(shows[offset : offset + 3])) == 3


def test_ordered_show_uses_episode_numbers_and_wraps(tmp_path, monkeypatch):
    episodes = ["S1E10.mp4", "S1E2.mp4", "S2E1.mp4", "S1E1.mp4"]
    database, config, _ = _station(
        tmp_path, monkeypatch, {"Pokemon": episodes}, ordered=("Pokemon",)
    )
    assert config.channels[0].ordered_shows == (tmp_path / "media" / "Pokemon",)
    Scheduler(database, config).fill(START)

    names = [Path(row["path"]).name for row in _programs(database)]
    expected = ["S1E1.mp4", "S1E2.mp4", "S1E10.mp4", "S2E1.mp4"]
    assert names[:8] == expected * 2


def test_selection_history_survives_seven_day_schedule_pruning(tmp_path, monkeypatch):
    episodes = [f"S01E{episode:02}.mp4" for episode in range(1, 9)]
    database, config, _ = _station(
        tmp_path, monkeypatch, {"Pokemon": episodes}, duration=3600.0,
        ordered=("Pokemon",),
    )
    Scheduler(database, config).fill(START)
    before = _programs(database)
    expected = episodes[(episodes.index(Path(before[-1]["path"]).name) + 1) % len(episodes)]
    now = START + 9 * 86400
    monkeypatch.setattr("nostalgiabox.server.media.time.time", lambda: now)

    Scheduler(database, config).fill(now)

    after = _programs(database)
    assert min(row["starts_at"] for row in after) >= now
    assert Path(after[0]["path"]).name == expected
    with database.connect() as connection:
        remembered = connection.execute(
            "SELECT COUNT(*) FROM selection_history WHERE channel_number=2"
        ).fetchone()[0]
    assert remembered == len(episodes)
    restarted = Scheduler(database, config)
    assert restarted.now(2, now).path.name == expected


def test_ordered_run_continues_from_airing_after_refresh_and_restart(tmp_path, monkeypatch):
    episodes = ["S1E1.mp4", "S1E2.mp4", "S1E10.mp4", "S2E1.mp4"]
    database, config, indexer = _station(
        tmp_path, monkeypatch, {"Pokemon": episodes}, ordered=("Pokemon",)
    )
    Scheduler(database, config).fill(START)
    before = _programs(database)
    now = before[1]["starts_at"] + 1
    monkeypatch.setattr("nostalgiabox.server.media.time.time", lambda: now)

    indexer.refresh()
    Scheduler(database, config).fill(now)

    names = [Path(row["path"]).name for row in _programs(database)]
    assert names[:8] == episodes * 2


def test_new_episode_joins_shuffle_before_aired_episodes_repeat(tmp_path, monkeypatch):
    episodes = [f"S01E{episode:02}.mp4" for episode in range(1, 5)]
    database, config, indexer = _station(
        tmp_path, monkeypatch, {"Arthur": episodes}
    )
    Scheduler(database, config).fill(START)
    before = _programs(database)
    now = before[1]["starts_at"] + 1
    monkeypatch.setattr("nostalgiabox.server.media.time.time", lambda: now)
    new_episode = tmp_path / "media" / "Arthur" / "S01E05.mp4"
    new_episode.write_bytes(b"completed media")

    indexer.refresh()
    Scheduler(database, config).fill(now)

    first_cycle = [Path(row["path"]).name for row in _programs(database)[:5]]
    assert len(set(first_cycle)) == 5
    assert new_episode.name in first_cycle


def test_ordered_run_includes_new_episode_in_its_numeric_position(tmp_path, monkeypatch):
    database, config, indexer = _station(
        tmp_path,
        monkeypatch,
        {"Pokemon": ["S1E1.mp4", "S1E3.mp4", "S1E4.mp4"]},
        ordered=("Pokemon",),
    )
    Scheduler(database, config).fill(START)
    now = START + 1
    monkeypatch.setattr("nostalgiabox.server.media.time.time", lambda: now)
    (tmp_path / "media" / "Pokemon" / "S1E2.mp4").write_bytes(b"completed media")

    indexer.refresh()
    Scheduler(database, config).fill(now)

    names = [Path(row["path"]).name for row in _programs(database)]
    assert names[:4] == ["S1E1.mp4", "S1E2.mp4", "S1E3.mp4", "S1E4.mp4"]


def test_configured_bumpers_rotate_between_episodes(tmp_path, monkeypatch):
    from dataclasses import replace

    database, config, _ = _station(tmp_path, monkeypatch, {'Arthur': ['S01E01.mp4']})
    bumper_folder = tmp_path / 'media' / 'Bumpers'
    bumper_folder.mkdir()
    for number in range(3):
        (bumper_folder / f'bumper-{number}.mp4').write_bytes(b'bumper')
    channel = replace(config.channels[0], bumpers=(bumper_folder,))
    config = config.with_channels([channel])
    MediaIndexer(database, config, tmp_path / 'media').refresh()
    Scheduler(database, config).fill(START)
    with database.connect() as connection:
        rows = connection.execute('SELECT kind, media_id FROM programs ORDER BY starts_at').fetchall()
    assert rows[0]['kind'] == 'show'
    assert all(left['kind'] != right['kind'] for left, right in zip(rows, rows[1:]))
    bumper_ids = [row['media_id'] for row in rows if row['kind'] == 'bumper']
    assert len(bumper_ids) >= 6
    assert len(set(bumper_ids[:3])) == len(set(bumper_ids[3:6])) == 3
