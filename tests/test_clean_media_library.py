import importlib.util
import sys
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "clean-media-library.py"
SPEC = importlib.util.spec_from_file_location("clean_media_library", SCRIPT)
cleanup = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = cleanup
SPEC.loader.exec_module(cleanup)


def test_renames_and_organizes_episode_in_one_plan(tmp_path):
    source_root = tmp_path / "downloads"
    show = source_root / "dragon-tales"
    show.mkdir(parents=True)
    source = show / "Dragon Tales.S01E14.The Name of the Episode.480p.WEB-DL.mp4"
    source.touch()

    moves, reviews = cleanup.build_plan(source_root, tmp_path / "media")

    assert reviews == []
    assert moves[0].destination == (
        tmp_path
        / "media/Shows/Dragon Tales (1999)/Season 01"
        / "Dragon Tales (1999) - S01E14 - The Name of the Episode.mp4"
    )


def test_organizes_year_tagged_movie(tmp_path):
    source_root = tmp_path / "downloads"
    source_root.mkdir()
    source = source_root / "The.Iron.Giant (1999).mp4"
    source.touch()

    moves, reviews = cleanup.build_plan(source_root, tmp_path / "media")

    assert reviews == []
    assert moves[0].destination == (
        tmp_path / "media/Movies/The Iron Giant (1999)/The Iron Giant (1999).mp4"
    )


def test_ambiguous_filename_is_sent_to_review(tmp_path):
    source_root = tmp_path / "downloads"
    show = source_root / "dragon-tales"
    show.mkdir(parents=True)
    source = show / "dragon-tales-episode-14-final-fixed.mp4"
    source.touch()

    moves, reviews = cleanup.build_plan(source_root, tmp_path / "media")

    assert moves == []
    assert reviews[0].source == source
    assert "metadata" in reviews[0].reason
