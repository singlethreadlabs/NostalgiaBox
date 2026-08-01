import importlib.util
import sys
from pathlib import Path

import pytest


SCRIPT = Path(__file__).parents[1] / "scripts" / "organize-media-library.py"
SPEC = importlib.util.spec_from_file_location("organize_media_library", SCRIPT)
organizer = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = organizer
SPEC.loader.exec_module(organizer)


def test_episode_uses_show_year_and_season_directory(tmp_path):
    source = tmp_path / "downloads" / "dragon-tales"
    source.mkdir(parents=True)
    episode = source / "Dragon Tales - S01E14 - The Big Sleep Over.mp4"
    episode.touch()

    moves, review = organizer.build_plan(tmp_path / "downloads", tmp_path / "media")

    assert review == []
    assert moves[0].destination == (
        tmp_path
        / "media/Shows/Dragon Tales (1999)/Season 01"
        / "Dragon Tales (1999) - S01E14 - The Big Sleep Over.mp4"
    )


def test_movie_uses_title_year_directory(tmp_path):
    source = tmp_path / "downloads"
    source.mkdir()
    movie = source / "The Iron Giant (1999).mkv"
    movie.touch()

    moves, review = organizer.build_plan(source, tmp_path / "media")

    assert review == []
    assert moves[0].destination == tmp_path / "media/Movies/The Iron Giant (1999)/The Iron Giant (1999).mkv"


def test_unknown_names_are_reviewed_and_partial_files_are_skipped(tmp_path):
    source = tmp_path / "downloads"
    source.mkdir()
    unknown = source / "dragon-tales-episode-14-final-fixed.mp4"
    unknown.touch()
    (source / "encoding.mp4.part").touch()

    moves, review = organizer.build_plan(source, tmp_path / "media")

    assert moves == []
    assert review == [unknown]


def test_collision_is_rejected(tmp_path):
    source = tmp_path / "downloads"
    source.mkdir()
    (source / "Dragon Tales - S01E01.mp4").touch()
    duplicate = source / "copy"
    duplicate.mkdir()
    (duplicate / "Dragon Tales (1999) - S01E01.mp4").touch()

    with pytest.raises(FileExistsError, match="destination collision"):
        organizer.build_plan(source, tmp_path / "media")


def test_apply_and_undo_round_trip(tmp_path):
    source_root = tmp_path / "downloads"
    source_root.mkdir()
    source = source_root / "The Iron Giant (1999).mp4"
    source.write_bytes(b"movie")
    destination_root = tmp_path / "media"
    moves, _ = organizer.build_plan(source_root, destination_root)
    manifest = tmp_path / "organize-manifest.csv"

    organizer.apply(moves, source_root, destination_root, manifest)

    destination = destination_root / "Movies/The Iron Giant (1999)/The Iron Giant (1999).mp4"
    assert destination.read_bytes() == b"movie"
    assert organizer.undo(manifest, source_root, destination_root) == 1
    assert source.read_bytes() == b"movie"
