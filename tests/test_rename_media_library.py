import importlib.util
import sys
from pathlib import Path

import pytest


SCRIPT = Path(__file__).parents[1] / "scripts" / "rename-media-library.py"
SPEC = importlib.util.spec_from_file_location("rename_media_library", SCRIPT)
rename_media_library = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = rename_media_library
SPEC.loader.exec_module(rename_media_library)


def test_build_plan_standardizes_known_episode(tmp_path):
    root = tmp_path / "downloads"
    show = root / "arthur"
    show.mkdir(parents=True)
    source = show / "Arthur.S1E2.The Contest.1080p.WEB-DL.mp4"
    source.touch()

    plan = rename_media_library.build_plan(root)

    assert len(plan) == 1
    assert plan[0].confidence == "high"
    assert plan[0].destination.name == "Arthur - S01E02 - The Contest.mp4"


def test_build_plan_skips_channels_partial_and_non_video_files(tmp_path):
    root = tmp_path / "downloads"
    show = root / "arthur"
    channels = root / "channels"
    show.mkdir(parents=True)
    channels.mkdir()
    (show / "Arthur_S01E01.mp4.part").touch()
    (show / "notes.txt").touch()
    (channels / "Arthur_S01E01.mp4").touch()

    assert rename_media_library.build_plan(root) == []


def test_build_plan_rejects_collision(tmp_path):
    root = tmp_path / "downloads"
    show = root / "arthur"
    show.mkdir(parents=True)
    (show / "Arthur.S01E01.mp4").touch()
    (show / "Arthur - S01E01.mp4").touch()

    with pytest.raises(FileExistsError, match="rename collision"):
        rename_media_library.build_plan(root)


def test_apply_manifest_can_be_undone(tmp_path):
    root = tmp_path / "downloads"
    show = root / "arthur"
    show.mkdir(parents=True)
    source = show / "Arthur.S01E01.mp4"
    source.write_bytes(b"episode")
    plan = rename_media_library.build_plan(root)

    manifest = rename_media_library.apply(plan, root)

    destination = show / "Arthur - S01E01.mp4"
    assert destination.read_bytes() == b"episode"
    assert not source.exists()
    assert rename_media_library.undo(manifest, root) == 1
    assert source.read_bytes() == b"episode"
    assert not destination.exists()
