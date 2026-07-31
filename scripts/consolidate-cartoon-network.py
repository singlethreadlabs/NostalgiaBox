#!/usr/bin/env python3
"""Consolidate Google Drive Cartoon Network export shards safely."""

from __future__ import annotations

import argparse
import csv
import hashlib
import re
import shutil
from dataclasses import dataclass
from pathlib import Path


MEDIA_EXTENSIONS = {".avi", ".m4v", ".mkv", ".mov", ".mp4", ".webm"}


@dataclass(frozen=True)
class Move:
    source: Path
    destination: Path
    category: str
    duplicate_of: Path | None = None


def shard_number(path: Path) -> int:
    return 1 if path.name == "Cartoon Network" else int(path.name.rsplit(" ", 1)[1])


def source_files(downloads: Path) -> list[Path]:
    shards = sorted(
        (
            path
            for path in downloads.iterdir()
            if path.is_dir()
            and (path.name == "Cartoon Network" or re.fullmatch(r"Cartoon Network \d+", path.name))
        ),
        key=shard_number,
    )
    files = [path for shard in shards for path in shard.rglob("*") if path.is_file()]
    files.extend(downloads.glob("Toonami*.mp4"))
    return sorted(files, key=lambda path: str(path).lower())


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def clean_name(name: str) -> str:
    stem, suffix = Path(name).stem, Path(name).suffix
    stem = re.sub(r"-\d{3}$", "", stem)
    stem = stem.replace("New Year_s", "New Year's")
    return f"{stem}{suffix.lower()}"


def classify(source: Path, downloads: Path, destination: Path) -> tuple[Path, str]:
    relative = source.relative_to(downloads)
    parts = relative.parts
    cleaned = clean_name(source.name)

    if source.name == ".DS_Store":
        return destination / "_Source Metadata" / relative, "metadata"

    series_index = next(
        (index for index, part in enumerate(parts) if part in {"DragonBall (Toonami Edit)", "DragonBall Z (Toonami Edit)"}),
        None,
    )
    if series_index is not None:
        series = "Dragon Ball" if parts[series_index].startswith("DragonBall (") else "Dragon Ball Z"
        tail = list(parts[series_index + 1 : -1])
        tail = [re.sub(r"^Dragonball Z? - \d+-", "", part, flags=re.I) for part in tail]
        if source.suffix.lower() in MEDIA_EXTENSIONS:
            return destination.joinpath("Series", series, "Toonami Edit", *tail, cleaned), "episode"
        return destination.joinpath("_Source Metadata", series, *tail, source.name), "metadata"

    if "Toonami 1997" in parts:
        return destination / "Broadcast Blocks" / "1997" / cleaned, "broadcast"

    if source.suffix.lower() in MEDIA_EXTENSIONS and source.name.startswith("Toonami"):
        match = re.search(r"(?:19|20)\d{2}", source.name)
        year = match.group(0) if match else "Undated"
        return destination / "Broadcast Blocks" / year / cleaned, "broadcast"

    return destination / "_Source Metadata" / relative, "metadata"


def plan(downloads: Path, destination: Path) -> list[Move]:
    planned: list[Move] = []
    seen: dict[tuple[str, int, str], Path] = {}
    occupied: set[Path] = set()

    for source in source_files(downloads):
        target, category = classify(source, downloads, destination)
        key = (source.name, source.stat().st_size, digest(source))
        duplicate_of = seen.get(key)
        if duplicate_of is not None:
            relative = source.relative_to(downloads)
            target = destination / "_Review" / "Exact Duplicates" / relative
            category = "exact-duplicate"
        else:
            seen[key] = target

        if target in occupied or target.exists():
            raise FileExistsError(f"destination collision: {target}")
        occupied.add(target)
        planned.append(Move(source, target, category, duplicate_of))

    return planned


def write_manifest(moves: list[Move], destination: Path) -> Path:
    manifest = destination / "consolidation-manifest.csv"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    with manifest.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(("category", "source", "destination", "duplicate_of"))
        for move in moves:
            writer.writerow((move.category, move.source, move.destination, move.duplicate_of or ""))
    return manifest


def apply(moves: list[Move], destination: Path) -> None:
    manifest = write_manifest(moves, destination)
    for move in moves:
        move.destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(move.source, move.destination)
    print(f"Manifest: {manifest}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--downloads", type=Path, default=Path.home() / "Downloads")
    parser.add_argument("--destination", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    downloads = args.downloads.resolve()
    destination = (args.destination or downloads / "Cartoon Network Consolidated").resolve()
    moves = plan(downloads, destination)
    counts: dict[str, int] = {}
    for move in moves:
        counts[move.category] = counts.get(move.category, 0) + 1
    for category, count in sorted(counts.items()):
        print(f"{category}: {count}")
    print(f"total: {len(moves)}")
    print(f"destination: {destination}")
    if args.apply:
        apply(moves, destination)
    else:
        print("Dry run only; pass --apply to move files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
