import importlib.util
import sys
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "media-migration-manifest.py"
SPEC = importlib.util.spec_from_file_location("media_migration_manifest", SCRIPT)
migration = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = migration
SPEC.loader.exec_module(migration)


def media(path: Path, size: int = 1) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    return path


def test_safe_component_removes_exfat_forbidden_characters():
    assert migration.safe_component('Batman: The Animated Series?') == "Batman - The Animated Series"


def test_maps_absolute_dragon_ball_z_episode():
    assert migration.dbz_season_episode(1) == (1, 1)
    assert migration.dbz_season_episode(40) == (2, 1)
    assert migration.dbz_season_episode(291) == (9, 38)


def test_builds_canonical_episode_and_movie_destinations(tmp_path):
    episode = media(
        tmp_path / "Shows" / "Spider-Man (1994)" / "03x03 Attack of the Octobot.mp4"
    )
    movie = media(
        tmp_path / "Movies" / "Disney Channel Original Movies" / "Twitches.2005..mp4"
    )

    entries = migration.build_manifest(tmp_path)
    by_source = {entry.source: entry.destination for entry in entries}

    assert by_source[episode] == Path(
        "Shows/Spider-Man (1994)/Season 03/Spider-Man (1994) - S03E03 - Attack of the Octobot.mp4"
    )
    assert by_source[movie] == Path(
        "Movies/Disney Channel Original Movies/Twitches (2005)/Twitches (2005).mp4"
    )


def test_catdog_ambiguous_exports_receive_stable_unique_numbers(tmp_path):
    first = media(tmp_path / "Shows" / "CatDog (1998)" / "Season 01" / "CD_S1_D1.mp4")
    second = media(tmp_path / "Shows" / "CatDog (1998)" / "Season 01" / "CD_S1_D2.mp4")

    entries = migration.build_manifest(tmp_path)
    by_source = {entry.source: entry for entry in entries}

    assert by_source[first].destination.name == "CatDog (1998) - S01E01.mp4"
    assert by_source[second].destination.name == "CatDog (1998) - S01E02.mp4"
    assert by_source[first].confidence == "inferred"


def test_parses_dotted_release_episode_and_strips_release_tags(tmp_path):
    source = media(
        tmp_path
        / "Shows"
        / "Reading Rainbow (1983)"
        / "Reading.Rainbow.S07E01.Humphrey.the.Lost.Whale.480p.AMZN.WEB-DL.x264-RTN.mp4"
    )

    entry = next(item for item in migration.build_manifest(tmp_path) if item.source == source)

    assert entry.destination == Path(
        "Shows/Reading Rainbow (1983)/Season 07/Reading Rainbow (1983) - S07E01 - Humphrey the Lost Whale.mp4"
    )
    assert entry.confidence == "high"
