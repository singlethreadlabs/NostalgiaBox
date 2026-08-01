#!/usr/bin/env python3
"""Redirect channel symlinks after media files are moved into an organized library."""

from __future__ import annotations

import argparse
import csv
import os
import uuid
from pathlib import Path


def safe_relative(value: str, *, field: str) -> Path:
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"unsafe {field} path in manifest: {value}")
    return path


def relocation_map(
    manifest: Path, source_root: Path, organized_root: Path
) -> dict[Path, Path]:
    relocations: dict[Path, Path] = {}
    with manifest.open(newline="", encoding="utf-8") as stream:
        for row in csv.DictReader(stream):
            source = safe_relative(row["source"], field="source")
            destination = safe_relative(row["destination"], field="destination")
            original = (source_root / source).resolve()
            organized = (organized_root / destination).resolve()
            if original in relocations and relocations[original] != organized:
                raise ValueError(f"conflicting relocation for {source}")
            relocations[original] = organized
    return relocations


def relink(
    channels: Path,
    relocations: dict[Path, Path],
    *,
    dry_run: bool = False,
) -> tuple[int, list[Path]]:
    changed = 0
    unresolved: list[Path] = []
    for link in sorted(path for path in channels.rglob("*") if path.is_symlink()):
        current = (link.parent / os.readlink(link)).resolve()
        if current.exists():
            continue
        destination = relocations.get(current)
        if destination is None or not destination.is_file():
            unresolved.append(link)
            continue
        changed += 1
        if dry_run:
            continue
        relative = os.path.relpath(destination, link.parent)
        temporary = link.with_name(f".{link.name}.{uuid.uuid4().hex}.relink")
        temporary.symlink_to(relative)
        os.replace(temporary, link)
    return changed, unresolved


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--downloads", type=Path, default=Path("downloads"))
    parser.add_argument(
        "--source-root",
        type=Path,
        help="Root for manifest source paths (defaults to --downloads)",
    )
    parser.add_argument("--channels", type=Path)
    parser.add_argument("--organized-root", type=Path, default=Path("media"))
    parser.add_argument(
        "--manifest", type=Path, default=Path("media/cleanup-manifest.csv")
    )
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    downloads = args.downloads.resolve()
    source_root = (args.source_root or downloads).resolve()
    channels = (args.channels or downloads / "channels").resolve()
    organized_root = args.organized_root.resolve()
    manifest = args.manifest.resolve()
    if not channels.is_dir() or not organized_root.is_dir() or not manifest.is_file():
        print("error: channels, organized root, and cleanup manifest must exist")
        return 2
    relocations = relocation_map(manifest, source_root, organized_root)
    changed, unresolved = relink(channels, relocations, dry_run=args.dry_run)
    verb = "Would relink" if args.dry_run else "Relinked"
    print(f"{verb} {changed} channel entries.")
    print(f"Unresolved pre-existing links: {len(unresolved)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
