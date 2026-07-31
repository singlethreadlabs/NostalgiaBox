import importlib.util
import json
import sys
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "media-promote.py"
SPEC = importlib.util.spec_from_file_location("media_promote", SCRIPT)
promoter = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = promoter
SPEC.loader.exec_module(promoter)


def write_manifest(path, source, staged, *, failures=None):
    path.write_text(
        json.dumps(
            {
                "files": [
                    {
                        "source": str(source),
                        "output": str(staged),
                        "verified": True,
                    }
                ],
                "failures": failures or [],
            }
        )
    )


def test_load_promotions_requires_complete_clean_manifest(tmp_path):
    root = tmp_path / "show"
    root.mkdir()
    source = root / "episode.mp4"
    source.write_bytes(b"source")
    staged = tmp_path / "episode.mp4"
    staged.write_bytes(b"staged")
    manifest = tmp_path / "manifest.json"
    write_manifest(manifest, source, staged)

    rows = promoter.load_promotions(manifest, root, tmp_path / "backup")

    assert len(rows) == 1
    assert rows[0].backup == tmp_path / "backup" / "episode.mp4"


def test_promote_backs_up_and_replaces_source(tmp_path):
    source = tmp_path / "show" / "episode.mp4"
    source.parent.mkdir()
    source.write_bytes(b"original")
    staged = tmp_path / "staged" / "episode.mp4"
    staged.parent.mkdir()
    staged.write_bytes(b"optimized")
    backup_dir = tmp_path / "backup"
    item = promoter.Promotion(source, staged, backup_dir / "episode.mp4")

    promoter.promote([item], backup_dir)

    assert source.read_bytes() == b"optimized"
    assert item.backup.read_bytes() == b"original"
    assert (backup_dir / "promotion.json").is_file()
