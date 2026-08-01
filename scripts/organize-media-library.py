#!/usr/bin/env python3
"""Plan and apply a collision-safe Movies/Shows media library layout."""

from __future__ import annotations

import argparse
import csv
import re
import uuid
from dataclasses import dataclass
from pathlib import Path


VIDEO_EXTENSIONS = {".avi", ".m4v", ".mkv", ".mov", ".mp4", ".webm"}

# Original premiere years used in canonical folder and file names.
SHOW_YEARS = {
    "Allegra's Window": 1994,
    "American Dragon: Jake Long": 2005,
    "Arthur": 1996,
    "Blue's Clues": 1996,
    "Courage the Cowardly Dog": 1999,
    "Dexter's Laboratory": 1996,
    "Dragon Ball": 1986,
    "Dragon Ball Z": 1989,
    "Dragon Tales": 1999,
    "Even Stevens": 2000,
    "Gullah Gullah Island": 1994,
    "Hannah Montana": 2006,
    "Inspector Gadget": 1983,
    "Iron Man": 1994,
    "KaBlam!": 1996,
    "Kim Possible": 2002,
    "Lilo & Stitch": 2003,
    "Little Bill": 1999,
    "Lizzie McGuire": 2001,
    "Phil of the Future": 2004,
    "Pokémon": 1997,
    "Spider-Man Unlimited": 1999,
    "Recess": 1997,
    "Sonic X": 2003,
    "Super Mario World": 1991,
    "Teenage Mutant Ninja Turtles": 2003,
    "The Fairly OddParents": 2001,
    "The Legend of Zelda": 1989,
    "The Magic School Bus": 1994,
    "The New Adventures of Winnie the Pooh": 1988,
    "The Proud Family": 2001,
    "The Suite Life of Zack & Cody": 2005,
    "That's So Raven": 2003,
    "Wizards of Waverly Place": 2007,
    "X-Men": 1992,
}

EPISODE_PATTERN = re.compile(
    r"^(?P<show>.+?)(?: \((?P<year>\d{4})\))?\s+-\s+"
    r"S(?P<season>\d{1,2})E(?P<episode>\d{1,3})(?P<title>.*)$",
    re.IGNORECASE,
)
MOVIE_PATTERN = re.compile(r"^(?P<title>.+?)\s+\((?P<year>\d{4})\)$")


@dataclass(frozen=True)
class Move:
    source: Path
    destination: Path
    kind: str


def without_known_year(show: str) -> tuple[str, int | None]:
    match = re.match(r"^(.*?)\s+\((\d{4})\)$", show)
    if match:
        return match.group(1), int(match.group(2))
    return show, SHOW_YEARS.get(show)


def destination_for(path: Path, destination_root: Path) -> tuple[Path, str] | None:
    episode = EPISODE_PATTERN.match(path.stem)
    if episode:
        show, mapped_year = without_known_year(episode.group("show").strip())
        year = int(episode.group("year")) if episode.group("year") else mapped_year
        if year is None:
            return None
        season = int(episode.group("season"))
        number = int(episode.group("episode"))
        title = episode.group("title").strip()
        canonical_show = f"{show} ({year})"
        filename = f"{canonical_show} - S{season:02d}E{number:02d}"
        if title:
            filename += f" {title}"
        filename += path.suffix.lower()
        return destination_root / "Shows" / canonical_show / f"Season {season:02d}" / filename, "show"

    movie = MOVIE_PATTERN.match(path.stem)
    if movie:
        canonical_movie = f"{movie.group('title').strip()} ({movie.group('year')})"
        return destination_root / "Movies" / canonical_movie / f"{canonical_movie}{path.suffix.lower()}", "movie"
    return None


def build_plan(source_root: Path, destination_root: Path) -> tuple[list[Move], list[Path]]:
    moves: list[Move] = []
    review: list[Path] = []
    destinations: set[str] = set()

    for path in sorted(source_root.rglob("*"), key=lambda item: str(item).casefold()):
        if not path.is_file() or path.is_symlink():
            continue
        relative_parts = path.relative_to(source_root).parts
        if "channels" in relative_parts or path.suffix.lower() not in VIDEO_EXTENSIONS:
            continue
        if ".part" in path.suffixes or any(part.startswith(".") for part in relative_parts):
            continue

        result = destination_for(path, destination_root)
        if result is None:
            review.append(path)
            continue
        destination, kind = result
        key = str(destination).casefold()
        if key in destinations or destination.exists():
            raise FileExistsError(f"destination collision: {destination}")
        destinations.add(key)
        moves.append(Move(path, destination, kind))
    return moves, review


def write_review(paths: list[Path], source_root: Path, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(("source", "reason"))
        writer.writerows((path.relative_to(source_root), "unrecognized show, episode, or movie name") for path in paths)


def apply(moves: list[Move], source_root: Path, destination_root: Path, manifest: Path) -> None:
    manifest.parent.mkdir(parents=True, exist_ok=True)
    with manifest.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(("kind", "source", "destination"))
        writer.writerows(
            (move.kind, move.source.relative_to(source_root), move.destination.relative_to(destination_root))
            for move in moves
        )

    staged: list[tuple[Path, Path]] = []
    for move in moves:
        temporary = move.source.with_name(f".{uuid.uuid4().hex}.organize")
        move.source.rename(temporary)
        staged.append((temporary, move.destination))
    for temporary, destination in staged:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary.rename(destination)


def undo(manifest: Path, source_root: Path, destination_root: Path) -> int:
    with manifest.open(newline="", encoding="utf-8") as stream:
        rows = list(csv.DictReader(stream))
    moves: list[tuple[Path, Path]] = []
    for row in reversed(rows):
        current = destination_root / row["destination"]
        original = source_root / row["source"]
        if not current.is_file():
            raise FileNotFoundError(f"organized file is missing: {current}")
        if original.exists():
            raise FileExistsError(f"original path is occupied: {original}")
        moves.append((current, original))
    for current, original in moves:
        original.parent.mkdir(parents=True, exist_ok=True)
        current.rename(original)
    return len(moves)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", nargs="?", type=Path, default=Path("downloads"))
    parser.add_argument("--destination", type=Path, default=Path("media"))
    parser.add_argument("--apply", action="store_true", help="perform the planned moves")
    parser.add_argument("--undo", type=Path, metavar="MANIFEST", help="restore files from a manifest")
    args = parser.parse_args()

    source_root = args.source.resolve()
    destination_root = args.destination.resolve()
    manifest = destination_root / "organize-manifest.csv"
    review_path = destination_root / "organize-review.csv"
    if args.undo:
        restored = undo(args.undo.resolve(), source_root, destination_root)
        print(f"restored={restored}")
        return 0

    moves, review = build_plan(source_root, destination_root)
    shows = sum(move.kind == "show" for move in moves)
    movies = sum(move.kind == "movie" for move in moves)
    print(f"planned={len(moves)} shows={shows} movies={movies} review={len(review)}")
    if not args.apply:
        for move in moves[:40]:
            print(f"[{move.kind}] {move.source} -> {move.destination}")
        print("Dry run only; pass --apply to organize.")
        return 0

    apply(moves, source_root, destination_root, manifest)
    write_review(review, source_root, review_path)
    print(f"moved={len(moves)} manifest={manifest} review={review_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
