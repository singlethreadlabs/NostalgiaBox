#!/usr/bin/env python3
"""Rename and organize a media library in one collision-safe operation."""

from __future__ import annotations

import argparse
import csv
from datetime import datetime
import importlib.util
import sys
from dataclasses import dataclass
from pathlib import Path
import re


def load_script(module_name: str, filename: str):
    script = Path(__file__).with_name(filename)
    spec = importlib.util.spec_from_file_location(module_name, script)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {script}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


renamer = load_script("rename_media_library", "rename-media-library.py")
organizer = load_script("organize_media_library", "organize-media-library.py")


@dataclass(frozen=True)
class Review:
    source: Path
    suggestion: str
    reason: str


def build_plan(source_root: Path, destination_root: Path):
    moves = []
    reviews = []
    destinations: set[str] = set()

    for source in sorted(source_root.rglob("*"), key=lambda item: str(item).casefold()):
        if not source.is_file() or source.is_symlink():
            continue
        relative = source.relative_to(source_root)
        if "channels" in relative.parts or source.suffix.lower() not in renamer.VIDEO_EXTENSIONS:
            continue
        if ".part" in source.suffixes or any(part.startswith(".") for part in relative.parts):
            continue

        # A year-tagged, non-episode filename is already sufficient to identify a movie.
        movie = organizer.MOVIE_PATTERN.match(source.stem)
        if movie:
            movie_name = f"{renamer.words(movie.group('title'))} ({movie.group('year')}){source.suffix.lower()}"
            result = organizer.destination_for(source.with_name(movie_name), destination_root)
        else:
            result = organizer.destination_for(source, destination_root)
        confidence = "high" if result and result[1] == "movie" else None
        suggested_name = source.name

        if result is None:
            suggested_name, confidence = renamer.canonical_name(source, source_root)
            result = organizer.destination_for(source.with_name(suggested_name), destination_root)

        if result is None or confidence == "review":
            top = relative.parts[0]
            if top == "Cartoon Network Consolidated" and "Broadcast Blocks" in relative.parts:
                year = next((part for part in relative.parts if re.fullmatch(r"\d{4}|Undated", part)), "Undated")
                destination = destination_root / "Broadcasts" / "Toonami" / year / source.name
                result = (destination, "broadcast")
                confidence = "high"
            elif top == "nick-jr":
                result = (destination_root / "Archives" / "Nick Jr" / source.name, "archive")
                confidence = "high"
            elif top == "sonic-x" and "Special Features" in relative.parts:
                result = (destination_root / "Extras" / "Sonic X (2003)" / source.name, "extra")
                confidence = "high"
            elif top == "Cartoon Network Consolidated" and "Toonami Edit" in relative.parts:
                marker = relative.parts.index("Toonami Edit")
                series = relative.parts[marker - 1]
                tail = Path(*relative.parts[marker + 1:])
                result = (destination_root / "Archives" / "Toonami Edits" / series / tail, "archive")
                confidence = "high"
            elif top == "arthur" and "New Year's Eve" in source.name:
                result = (
                    destination_root / "Shows" / "Arthur (1996)" / "Season 00" /
                    f"Arthur (1996) - S00E01 - New Year's Eve{source.suffix.lower()}",
                    "show",
                )
                confidence = "high"
            elif top == "arthur" and "Perfect Christmas" in source.name:
                result = (
                    destination_root / "Shows" / "Arthur (1996)" / "Season 00" /
                    f"Arthur (1996) - S00E02 - Perfect Christmas{source.suffix.lower()}",
                    "show",
                )
                confidence = "high"
            elif top == "arthur" and "intro" in source.name.lower():
                result = (destination_root / "Extras" / "Arthur (1996)" / source.name, "extra")
                confidence = "high"
            elif top == "dexters-laboratory" and "Ego Trip" in source.name:
                result = (
                    destination_root / "Shows" / "Dexter's Laboratory (1996)" / "Season 00" /
                    f"Dexter's Laboratory (1996) - S00E01 - Ego Trip{source.suffix.lower()}",
                    "show",
                )
                confidence = "high"
            elif top == "kablam" and re.search(r"EP\s*48", source.stem, re.I):
                result = (
                    destination_root / "Shows" / "KaBlam! (1996)" / "Season 04" /
                    f"KaBlam! (1996) - S04E09{source.suffix.lower()}",
                    "show",
                )
                confidence = "high"
            elif top == "the-fairly-oddparents-the-complete-series_202507" and source.stem.startswith("SMx"):
                movies = {
                    "SMx1": ("A Fairly Odd Movie: Grow Up, Timmy Turner!", 2011),
                    "SMx2": ("A Fairly Odd Christmas", 2012),
                    "SMx3": ("A Fairly Odd Summer", 2014),
                }
                key = source.stem.split()[0]
                title, year = movies[key]
                canonical = f"{title} ({year})"
                result = (
                    destination_root / "Movies" / canonical / f"{canonical}{source.suffix.lower()}",
                    "movie",
                )
                confidence = "high"

        if result is None or confidence == "review":
            reason = "missing reliable show/year/season/episode metadata"
            reviews.append(Review(source, suggested_name, reason))
            continue

        destination, kind = result
        destination_key = str(destination).casefold()
        if destination_key in destinations or destination.exists():
            raise FileExistsError(f"destination collision: {destination}")
        destinations.add(destination_key)
        moves.append(organizer.Move(source, destination, kind))

    return moves, reviews


def write_review(reviews: list[Review], source_root: Path, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(("source", "suggested_filename", "reason"))
        writer.writerows(
            (review.source.relative_to(source_root), review.suggestion, review.reason)
            for review in reviews
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", nargs="?", type=Path, default=Path("downloads"))
    parser.add_argument("--destination", type=Path, default=Path("media"))
    parser.add_argument("--apply", action="store_true", help="perform all confident renames and moves")
    parser.add_argument("--undo", type=Path, metavar="MANIFEST", help="restore files from a manifest")
    args = parser.parse_args()

    source_root = args.source.resolve()
    destination_root = args.destination.resolve()
    manifest_dir = destination_root / "migrations"
    manifest = manifest_dir / f"cleanup-{datetime.now().strftime('%Y%m%d-%H%M%S')}.csv"
    review_path = destination_root / "cleanup-review.csv"

    if args.undo:
        restored = organizer.undo(args.undo.resolve(), source_root, destination_root)
        print(f"restored={restored}")
        return 0

    moves, reviews = build_plan(source_root, destination_root)
    show_count = sum(move.kind == "show" for move in moves)
    movie_count = sum(move.kind == "movie" for move in moves)
    print(
        f"planned={len(moves)} shows={show_count} movies={movie_count} "
        f"review={len(reviews)}"
    )
    if not args.apply:
        for move in moves[:40]:
            print(f"[{move.kind}] {move.source} -> {move.destination}")
        print("Dry run only; pass --apply to rename and organize.")
        return 0

    organizer.apply(moves, source_root, destination_root, manifest)
    write_review(reviews, source_root, review_path)
    print(f"moved={len(moves)} manifest={manifest} review={review_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
