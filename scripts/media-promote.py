#!/usr/bin/env python3
"""Atomically promote a complete verified staging manifest with rollback."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any


OPTIMIZER_PATH = Path(__file__).with_name("media-optimize.py")
SPEC = importlib.util.spec_from_file_location("media_optimize", OPTIMIZER_PATH)
optimizer = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = optimizer
SPEC.loader.exec_module(optimizer)


@dataclass(frozen=True)
class Promotion:
    source: Path
    staged: Path
    backup: Path


def load_promotions(
    manifest_path: Path, source_root: Path, backup_dir: Path
) -> list[Promotion]:
    data: dict[str, Any] = json.loads(manifest_path.read_text(encoding="utf-8"))
    if data.get("failures"):
        raise ValueError("manifest contains failures")

    root = source_root.resolve()
    source_files = set(optimizer.find_media([root]))
    rows = data.get("files") or []
    manifest_sources = {Path(row["source"]).resolve() for row in rows}
    if manifest_sources != source_files:
        missing = len(source_files - manifest_sources)
        extra = len(manifest_sources - source_files)
        raise ValueError(
            f"manifest is incomplete for source root: {missing} missing, {extra} extra"
        )

    promotions: list[Promotion] = []
    for row in rows:
        source = Path(row["source"]).resolve()
        staged = Path(row["output"]).resolve()
        if not row.get("verified"):
            raise ValueError(f"manifest entry is not verified: {source}")
        if not source.is_relative_to(root):
            raise ValueError(f"source is outside source root: {source}")
        if not source.is_file() or not staged.is_file():
            raise ValueError(f"source or staged output is missing: {source}")
        if source.suffix.lower() != staged.suffix.lower():
            raise ValueError(
                f"container extension change requires channel relinking: {source}"
            )
        promotions.append(
            Promotion(
                source=source,
                staged=staged,
                backup=backup_dir / source.relative_to(root),
            )
        )
    return sorted(promotions, key=lambda item: str(item.source).lower())


def atomic_copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(
        f".{destination.name}.nostalgiabox-{uuid.uuid4().hex}.tmp"
    )
    try:
        shutil.copy2(source, temporary)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def promote(promotions: list[Promotion], backup_dir: Path) -> None:
    backup_dir.mkdir(parents=True, exist_ok=False)
    for item in promotions:
        item.backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item.source, item.backup)

    promoted: list[Promotion] = []
    try:
        for item in promotions:
            atomic_copy(item.staged, item.source)
            promoted.append(item)
    except Exception:
        for item in reversed(promoted):
            atomic_copy(item.backup, item.source)
        raise

    record = {
        "status": "promoted",
        "files": [
            {
                "source": str(item.source),
                "staged": str(item.staged),
                "backup": str(item.backup),
            }
            for item in promotions
        ],
    }
    (backup_dir / "promotion.json").write_text(
        json.dumps(record, indent=2) + "\n", encoding="utf-8"
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Promote a complete verified media manifest with backups."
    )
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--backup-dir", required=True, type=Path)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="perform promotion; otherwise validate and print the plan",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.backup_dir.exists():
        print("error: --backup-dir must not already exist", file=sys.stderr)
        return 1
    try:
        promotions = load_promotions(
            args.manifest, args.source_root, args.backup_dir.resolve()
        )
        print(f"Validated {len(promotions)} files for atomic promotion.")
        print(f"Backup destination: {args.backup_dir}")
        if not args.apply:
            print("Dry run only; pass --apply to promote the staged files.")
            return 0
        promote(promotions, args.backup_dir)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(f"Promoted {len(promotions)} files; originals are in {args.backup_dir}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
