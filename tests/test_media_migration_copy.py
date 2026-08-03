import csv
import importlib.util
import sys
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "media-migration-copy.py"
SPEC = importlib.util.spec_from_file_location("media_migration_copy", SCRIPT)
copy = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = copy
SPEC.loader.exec_module(copy)


def test_copy_one_verifies_identical_sha256(tmp_path):
    source = tmp_path / "source.mp4"
    destination = tmp_path / "ssd" / "episode.mp4"
    source.write_bytes(b"video" * 1024)

    record = copy.copy_one(source, destination, source.stat().st_size)

    assert destination.read_bytes() == source.read_bytes()
    assert record["status"] == "verified"
    assert record["sha256"] == copy.hash_file(source)


def test_completed_destinations_ignores_failures(tmp_path):
    journal = tmp_path / "journal.jsonl"
    journal.write_text(
        '{"status":"verified","destination":"/a.mp4"}\n'
        '{"status":"failed","destination":"/b.mp4"}\n',
        encoding="utf-8",
    )

    assert set(copy.completed_destinations(journal)) == {"/a.mp4"}


def test_manifest_rejects_case_insensitive_collision(tmp_path):
    manifest = tmp_path / "manifest.csv"
    with manifest.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(("source", "destination", "size", "confidence", "reason"))
        writer.writerow(("a.mp4", "Shows/A.mp4", 1, "high", "test"))
        writer.writerow(("b.mp4", "shows/a.mp4", 1, "high", "test"))

    try:
        copy.load_manifest(manifest)
    except ValueError as error:
        assert "collision" in str(error)
    else:
        raise AssertionError("expected collision error")
