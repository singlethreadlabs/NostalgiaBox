import csv
import importlib.util
import sys
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "relink-organized-channels.py"
SPEC = importlib.util.spec_from_file_location("relink_organized_channels", SCRIPT)
module = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = module
SPEC.loader.exec_module(module)


def write_manifest(path: Path, source: str, destination: str) -> None:
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=("kind", "source", "destination"))
        writer.writeheader()
        writer.writerow({"kind": "show", "source": source, "destination": destination})


def test_relinks_moved_file_and_preserves_working_link(tmp_path):
    downloads = tmp_path / "downloads"
    channels = downloads / "channels" / "01-test" / "Show"
    organized = tmp_path / "media"
    moved = organized / "Shows" / "Show (2000)" / "Season 01" / "Show - S01E01.mp4"
    moved.parent.mkdir(parents=True)
    moved.write_bytes(b"video")
    channels.mkdir(parents=True)
    moved_link = channels / "Show - S01E01.mp4"
    moved_link.symlink_to("../../../show/Show - S01E01.mp4")
    existing = downloads / "other" / "Existing.mp4"
    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"existing")
    working_link = channels / "Existing.mp4"
    working_target = "../../../other/Existing.mp4"
    working_link.symlink_to(working_target)
    manifest = organized / "cleanup-manifest.csv"
    write_manifest(
        manifest,
        "show/Show - S01E01.mp4",
        "Shows/Show (2000)/Season 01/Show - S01E01.mp4",
    )

    relocations = module.relocation_map(manifest, downloads, organized)
    changed, unresolved = module.relink(channels.parent.parent, relocations)

    assert changed == 1
    assert unresolved == []
    assert moved_link.resolve() == moved.resolve()
    assert working_link.readlink() == Path(working_target)


def test_dry_run_does_not_change_link(tmp_path):
    downloads = tmp_path / "downloads"
    channels = downloads / "channels"
    channels.mkdir(parents=True)
    link = channels / "episode.mp4"
    original_target = "../show/episode.mp4"
    link.symlink_to(original_target)
    destination = tmp_path / "media" / "Shows" / "Show" / "episode.mp4"
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"video")
    relocations = {
        (downloads / "show" / "episode.mp4").resolve(): destination.resolve()
    }

    changed, unresolved = module.relink(channels, relocations, dry_run=True)

    assert changed == 1
    assert unresolved == []
    assert link.readlink() == Path(original_target)


def test_rejects_manifest_path_traversal(tmp_path):
    manifest = tmp_path / "manifest.csv"
    write_manifest(manifest, "../outside.mp4", "Shows/Show/episode.mp4")

    try:
        module.relocation_map(manifest, tmp_path / "downloads", tmp_path / "media")
    except ValueError as error:
        assert "unsafe source" in str(error)
    else:
        raise AssertionError("expected unsafe manifest path to fail")
