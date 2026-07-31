import importlib.util
import sys
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "channel-shuffle.py"
SPEC = importlib.util.spec_from_file_location("channel_shuffle", SCRIPT)
channel_shuffle = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = channel_shuffle
SPEC.loader.exec_module(channel_shuffle)


def test_build_groups_media_without_moving_sources(tmp_path):
    downloads = tmp_path / "downloads"
    disney = downloads / "disney-channel"
    disney.mkdir(parents=True)
    source = disney / "Kim Possible S01E01.mp4"
    source.write_bytes(b"episode")
    (disney / "Kim Possible S01E02.mp4.part").write_bytes(b"partial")

    destination = downloads / "channels"
    linked, counts = channel_shuffle.build(downloads, destination)

    assert linked == 3
    assert counts["08-city-toons"] == 1
    assert counts["09-nova-action"] == 0
    assert counts["13-powerhouse-kids"] == 1
    assert counts["15-saturday-club"] == 1
    link = destination / "13-powerhouse-kids" / "Kim Possible" / source.name
    assert link.is_symlink()
    assert link.resolve() == source
    assert source.read_bytes() == b"episode"


def test_build_is_idempotent(tmp_path):
    source_dir = tmp_path / "downloads" / "arthur"
    source_dir.mkdir(parents=True)
    (source_dir / "Arthur-S01E01.mp4").touch()

    first, _ = channel_shuffle.build(tmp_path / "downloads", tmp_path / "lineup")
    second, _ = channel_shuffle.build(tmp_path / "downloads", tmp_path / "lineup")

    assert first == 3
    assert second == 0


def test_build_removes_stale_generated_links(tmp_path):
    downloads = tmp_path / "downloads"
    source_dir = downloads / "arthur"
    source_dir.mkdir(parents=True)
    source = source_dir / "Arthur-S01E01.mp4"
    source.touch()
    destination = downloads / "channels"
    stale = destination / "99-old" / "Old Show" / "old.mp4"
    stale.parent.mkdir(parents=True)
    stale.symlink_to(source)

    channel_shuffle.build(downloads, destination)

    assert not stale.exists()
    assert not stale.is_symlink()


def test_dry_run_does_not_change_generated_tree(tmp_path):
    downloads = tmp_path / "downloads"
    source_dir = downloads / "arthur"
    source_dir.mkdir(parents=True)
    (source_dir / "Arthur-S01E01.mp4").touch()
    destination = downloads / "channels"

    linked, _ = channel_shuffle.build(downloads, destination, dry_run=True)

    assert linked == 3
    assert not destination.exists()


def test_unexpected_regular_file_is_rejected(tmp_path):
    downloads = tmp_path / "downloads"
    (downloads / "arthur").mkdir(parents=True)
    destination = downloads / "channels"
    destination.mkdir()
    (destination / "user-file.txt").write_text("preserve me")

    import pytest

    with pytest.raises(ValueError, match="unexpected regular file"):
        channel_shuffle.build(downloads, destination)


def test_primary_action_channels_are_disjoint():
    nova = set(channel_shuffle.CHANNELS["09-nova-action"])
    powerhouse = set(channel_shuffle.CHANNELS["13-powerhouse-kids"])

    assert nova.isdisjoint(powerhouse)


def test_classification_accounts_for_duplicates_bumpers_and_reviews(tmp_path):
    downloads = tmp_path / "downloads"
    duplicate = downloads / "Cartoon Network Consolidated" / "_Review" / "Exact Duplicates" / "copy.mp4"
    bumper = downloads / "nick-jr" / "Nick Jr Promo.mp4"
    unknown = downloads / "nick-jr" / "mystery.mp4"
    for path in (duplicate, bumper, unknown):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()

    rows = channel_shuffle.classify([duplicate, bumper, unknown], downloads)

    assert [row["kind"] for row in rows] == ["excluded", "bumper", "review"]
    assert all(row["reason"] for row in rows)
