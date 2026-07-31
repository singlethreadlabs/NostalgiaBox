import random

from nostalgiabox.channel import (
    BroadcastSchedule,
    Channel,
    ChannelLineup,
    build_lineup,
    detect_season,
    scan_episodes,
)
from nostalgiabox.config import config_from_dict
from tests.helpers import make_show


def _channel(tmp_path, name="arthur", episodes=4, **kw):
    folder = make_show(tmp_path, name, episodes)
    from nostalgiabox.config import ChannelConfig

    cfg = ChannelConfig(number=kw.pop("number", 3), name=name, path=folder)
    eps = scan_episodes(folder, [".mp4"])
    return Channel(cfg, eps, rng=random.Random(0), **kw)


def test_scan_episodes_sorted_and_filtered(tmp_path):
    folder = make_show(tmp_path, "arthur", 3)
    (folder / "notes.txt").write_text("nope")
    (folder / ".DS_Store").write_bytes(b"")
    eps = scan_episodes(folder, [".mp4"])
    assert [p.name for p in eps] == [
        "arthur_ep01.mp4",
        "arthur_ep02.mp4",
        "arthur_ep03.mp4",
    ]


def test_detect_season():
    assert detect_season("Arthur S06E01.mp4") == 6
    assert detect_season("arthur.s6e12.mkv") == 6
    assert detect_season("Season 12/ep03.mp4") == 12
    assert detect_season("Arthur 6x05.mp4") == 6
    assert detect_season("Arthurs Perfect Christmas.mp4") is None


def test_scan_exclude_globs(tmp_path):
    folder = tmp_path / "arthur"
    (folder / "Season 1").mkdir(parents=True)
    (folder / "Specials").mkdir(parents=True)
    (folder / "Season 1" / "S01E01.mp4").write_bytes(b"")
    (folder / "Specials" / "Arthur Special.mp4").write_bytes(b"")
    eps = scan_episodes(folder, [".mp4"], exclude=["*special*"])
    names = [p.name for p in eps]
    assert names == ["S01E01.mp4"]


def test_scan_exclude_seasons(tmp_path):
    folder = tmp_path / "arthur"
    folder.mkdir()
    for s in (1, 5, 6, 7, 25):
        (folder / f"Arthur S{s:02d}E01.mp4").write_bytes(b"")
    eps = scan_episodes(folder, [".mp4"], exclude_seasons=set(range(6, 26)))
    seasons = sorted(detect_season(p.name) for p in eps)
    assert seasons == [1, 5]  # 6..25 removed


def test_build_lineup_applies_channel_excludes(tmp_path):
    folder = tmp_path / "arthur"
    folder.mkdir()
    (folder / "Arthur S01E01.mp4").write_bytes(b"")
    (folder / "Arthur S06E01.mp4").write_bytes(b"")
    (folder / "Arthur Special.mp4").write_bytes(b"")
    cfg = config_from_dict(
        {
            "channels": [
                {
                    "number": 3,
                    "name": "Arthur",
                    "path": str(folder),
                    "exclude": ["*special*"],
                    "exclude_seasons": ["6-25"],
                }
            ]
        }
    )
    lineup = build_lineup(cfg)
    eps = list(lineup)[0].episodes
    assert [p.name for p in eps] == ["Arthur S01E01.mp4"]


def test_scan_recursive(tmp_path):
    base = tmp_path / "show"
    (base / "season1").mkdir(parents=True)
    (base / "season2").mkdir(parents=True)
    (base / "season1" / "a.mp4").write_bytes(b"")
    (base / "season2" / "b.mp4").write_bytes(b"")
    assert len(scan_episodes(base, [".mp4"], recursive=True)) == 2
    assert len(scan_episodes(base, [".mp4"], recursive=False)) == 0


def test_tune_in_random_plays_from_start(tmp_path):
    ch = _channel(tmp_path, tune_in="random")
    req = ch.tune_in()
    assert req is not None
    assert req.start == 0.0
    assert req.path in ch.episodes


def test_advance_continues_shuffle(tmp_path):
    ch = _channel(tmp_path, episodes=4, tune_in="random")
    seen = {ch.tune_in().path}
    for _ in range(3):
        seen.add(ch.advance().path)
    assert len(seen) == 4  # every episode shown before repeats


def test_advance_balances_shows_before_repeating(tmp_path):
    large = make_show(tmp_path, "large", 8)
    small = make_show(tmp_path, "small", 1)
    from nostalgiabox.config import ChannelConfig

    pools = {
        str(large): scan_episodes(large, [".mp4"]),
        str(small): scan_episodes(small, [".mp4"]),
    }
    episodes = [episode for pool in pools.values() for episode in pool]
    config = ChannelConfig(number=3, name="Balanced", path=large, shows=(large, small))
    channel = Channel(config, episodes, rng=random.Random(0), episode_pools=pools)

    first_round = [channel.tune_in().path.parent, channel.advance().path.parent]
    second_round = [channel.advance().path.parent, channel.advance().path.parent]

    assert set(first_round) == {large, small}
    assert set(second_round) == {large, small}


def test_build_lineup_uses_explicit_show_pools(tmp_path):
    large = make_show(tmp_path, "large", 5)
    small = make_show(tmp_path, "small", 1)
    config = config_from_dict(
        {"channels": [{"number": 3, "name": "Balanced", "shows": [str(large), str(small)]}]}
    )

    channel = list(build_lineup(config, rng=random.Random(0)))[0]
    selected = [channel.tune_in().path.parent, channel.advance().path.parent]

    assert set(selected) == {large, small}


def test_start_offset_fixed(tmp_path):
    ch = _channel(tmp_path, tune_in="random", start_offset_min=5.0, start_offset_max=5.0)
    assert ch.tune_in().start == 5.0
    assert ch.advance().start == 5.0


def test_start_offset_range(tmp_path):
    ch = _channel(tmp_path, tune_in="random", start_offset_min=6.0, start_offset_max=10.0)
    starts = [ch.tune_in().start for _ in range(20)] + [ch.advance().start for _ in range(20)]
    assert all(6.0 <= s <= 10.0 for s in starts)
    assert len(set(round(s, 3) for s in starts)) > 1  # actually varies


def test_resume_mode_remembers_position(tmp_path):
    ch = _channel(tmp_path, tune_in="resume")
    first = ch.tune_in()
    ch.remember(first.path, 123.5)
    again = ch.tune_in()
    assert again.path == first.path
    assert again.start == 123.5


def test_empty_channel_returns_none(tmp_path):
    folder = tmp_path / "empty"
    folder.mkdir()
    from nostalgiabox.config import ChannelConfig

    ch = Channel(ChannelConfig(number=9, name="Empty", path=folder), [])
    assert ch.is_empty
    assert ch.tune_in() is None
    assert ch.advance() is None


def test_broadcast_schedule_positions():
    from pathlib import Path

    eps = [Path("a.mp4"), Path("b.mp4"), Path("c.mp4")]
    durs = [100.0, 200.0, 300.0]
    sched = BroadcastSchedule(eps, durs, epoch=0.0, rng=random.Random(0))
    # At t=0 we are at the start of the first item in the (shuffled) order.
    first = sched.at(0.0)
    assert first.start == 0.0
    # The schedule is a loop of total length 600s; t=600 == t=0.
    assert sched.at(600.0).path == first.path
    # 50s into the cycle we should still be within the first item, offset 50.
    assert sched.at(50.0).start == 50.0


def test_broadcast_schedule_balances_uneven_show_pools():
    from pathlib import Path

    small = [Path("small/one.mp4")]
    large = [Path(f"large/{index}.mp4") for index in range(4)]
    episodes = small + large
    schedule = BroadcastSchedule(
        episodes,
        [60.0] * len(episodes),
        epoch=0.0,
        rng=random.Random(0),
        episode_pools={"small": small, "large": large},
    )

    parents = [schedule.at(index * 60).path.parent.name for index in range(8)]
    assert parents.count("small") == parents.count("large") == 4
    assert all(left != right for left, right in zip(parents, parents[1:]))


def test_broadcast_tune_in_uses_real_time(tmp_path, monkeypatch):
    # Force probe_duration to a known value so we don't need ffprobe/real media.
    import nostalgiabox.channel as channel_mod

    monkeypatch.setattr(channel_mod, "probe_duration", lambda p: 60.0)
    ch = _channel(tmp_path, episodes=3, tune_in="broadcast")
    # Two tune-ins at different times should generally land at different offsets.
    r1 = ch.tune_in(now=0.0)
    r2 = ch.tune_in(now=30.0)
    assert r1.start == 0.0
    assert r2.start == 30.0


def test_lineup_navigation(tmp_path):
    for n in ("a", "b", "c"):
        make_show(tmp_path, n, 1)
    cfg = config_from_dict(
        {
            "shuffle_seed": 1,
            "channels": [
                {"number": 2, "name": "A", "path": str(tmp_path / "a")},
                {"number": 4, "name": "B", "path": str(tmp_path / "b")},
                {"number": 7, "name": "C", "path": str(tmp_path / "c")},
            ],
        }
    )
    lineup = build_lineup(cfg)
    assert lineup.numbers == [2, 4, 7]
    assert lineup.current.number == 2
    assert lineup.up().number == 4
    assert lineup.up().number == 7
    assert lineup.up().number == 2  # wraps
    assert lineup.down().number == 7  # wraps back
    assert lineup.select_number(4).number == 4
    assert lineup.select_number(99) is None
    assert lineup.has_number(7)


def test_lineup_sorted_by_number(tmp_path):
    for n in ("a", "b"):
        make_show(tmp_path, n, 1)
    cfg = config_from_dict(
        {
            "channels": [
                {"number": 9, "name": "Nine", "path": str(tmp_path / "a")},
                {"number": 3, "name": "Three", "path": str(tmp_path / "b")},
            ]
        }
    )
    lineup = build_lineup(cfg)
    assert lineup.numbers == [3, 9]
