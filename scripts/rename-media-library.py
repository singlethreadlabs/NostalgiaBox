#!/usr/bin/env python3
"""Plan and apply collision-safe canonical media filenames."""

from __future__ import annotations

import argparse
import csv
import re
import uuid
from dataclasses import dataclass
from pathlib import Path


VIDEO_EXTENSIONS = {".avi", ".m4v", ".mkv", ".mov", ".mp4", ".webm"}
SHOWS = {
    "arthur": "Arthur", "disney-channel": None, "dragon-tales": "Dragon Tales",
    "kablam": "KaBlam!", "magic-schoolbus": "The Magic School Bus",
    "the-fairly-oddparents-the-complete-series_202507": "The Fairly OddParents",
    "tmnt-2003": "Teenage Mutant Ninja Turtles (2003)", "sonic-x": "Sonic X",
    "inspector-gadget": "Inspector Gadget", "dexters-laboratory": "Dexter's Laboratory",
    "x-men": "X-Men", "courage-the-cowardly-dog": "Courage the Cowardly Dog",
    "pokemon": "Pokémon", "iron-man-1994": "Iron Man (1994)",
    "legend-of-zelda": "The Legend of Zelda", "super-mario-world": "Super Mario World",
}


@dataclass(frozen=True)
class Rename:
    source: Path
    destination: Path
    confidence: str


def strip_release_tags(value: str) -> str:
    value = re.sub(r"\s*\[[^]]*(?:p|rip|web|x26|sx|rcvr)[^]]*]\s*", " ", value, flags=re.I)
    value = re.sub(r"\s*\((?:\d{3,4}p[^)]*|\d{4}p[^)]*|480p[^)]*)\)\s*", " ", value, flags=re.I)
    value = re.sub(r"[._](?:CBS|CBSA|DSNP)[._-]WEB-DL.*$", "", value, flags=re.I)
    value = re.sub(r"\s+(?:DSNP|WEB-DL|DVDRip|x26[45]).*$", "", value, flags=re.I)
    return value.strip(" ._-")


def words(value: str) -> str:
    value = value.replace("_s", "'s").replace("_", " ")
    if "." in value and " " not in value:
        value = value.replace(".", " ")
    value = re.sub(r"\s+", " ", value).strip(" ._-")
    return value


def season_episode(folder: str, number: int) -> tuple[int | None, int]:
    boundaries = {
        "magic-schoolbus": (13, 13, 13, 13),
        "x-men": (13, 13, 19, 17, 14),
        "iron-man-1994": (13, 13),
    }.get(folder)
    if not boundaries:
        return None, number
    remaining = number
    for season, count in enumerate(boundaries, 1):
        if remaining <= count:
            return season, remaining
        remaining -= count
    return None, number


def infer_disney(stem: str) -> str | None:
    patterns = (
        (r"^American Dragon", "American Dragon: Jake Long"), (r"^Even Stevens", "Even Stevens"),
        (r"^Hannah[ .]Montana", "Hannah Montana"), (r"^Kim Possible", "Kim Possible"),
        (r"^Lizzie", "Lizzie McGuire"), (r"^Lilo", "Lilo & Stitch"),
        (r"^Recess", "Recess"), (r"^That.?s So Raven", "That's So Raven"),
        (r"^The Proud Family", "The Proud Family"), (r"^The Suite Life", "The Suite Life of Zack & Cody"),
        (r"^Wizards of Waverly Place", "Wizards of Waverly Place"),
        (r"^Phil of the Future", "Phil of the Future"),
        (r"^The.New.Adventures.of.Winnie.the.Pooh", "The New Adventures of Winnie the Pooh"),
    )
    return next((name for pattern, name in patterns if re.search(pattern, stem, re.I)), None)


def canonical_name(path: Path, root: Path) -> tuple[str, str]:
    relative = path.relative_to(root)
    folder = relative.parts[0]
    stem = strip_release_tags(path.stem)
    show = SHOWS.get(folder)
    if folder == "disney-channel":
        show = infer_disney(stem)
    if folder == "nick-jr":
        if re.search(r"gullah", stem, re.I): show = "Gullah Gullah Island"
        elif re.search(r"blue.?s clues", stem, re.I): show = "Blue's Clues"
        elif re.search(r"allegra", stem, re.I): show = "Allegra's Window"
        elif re.search(r"little bill", stem, re.I): show = "Little Bill"
    if "Dragon Ball Z" in relative.parts:
        show = "Dragon Ball Z"
    elif "Dragon Ball" in relative.parts:
        show = "Dragon Ball"

    match = re.search(r"(?<![A-Za-z0-9])S(\d{1,2})[ ._-]*E(\d{1,3})(?:E\d{1,3})?", stem, re.I)
    if not match:
        match = re.search(r"(?<!\d)(\d{1,2})x(\d{1,3})(?!\d)", stem, re.I)
    season = episode = None
    if match:
        season, episode = int(match.group(1)), int(match.group(2))
        title = stem[match.end():]
    else:
        showless = re.sub(rf"^{re.escape(show or '')}\s*", "", stem, flags=re.I) if show else stem
        global_match = re.match(r"(?:Episode|EP|E)?\s*(\d{1,3})\s*[- ]+\s*(.*)", showless, re.I)
        if global_match and show:
            season, episode = season_episode(folder, int(global_match.group(1)))
            title = global_match.group(2)
        else:
            title = stem

    if show and episode is not None:
        title = re.sub(r"^\s*[-–—]+\s*", "", title)
        title = re.sub(rf"^{re.escape(show)}(?:\s*\(\d{{4}}\))?\s*[- ]*", "", title, flags=re.I)
        title = words(title)
        code = f"S{season:02d}E{episode:02d}" if season is not None else f"E{episode:03d}"
        return f"{show} - {code}" + (f" - {title}" if title else "") + path.suffix.lower(), "high"

    cleaned = words(stem) + path.suffix.lower()
    return cleaned, "review" if not show else "medium"


def build_plan(root: Path) -> list[Rename]:
    renames = []
    occupied: set[str] = set()
    for path in sorted(root.rglob("*"), key=lambda item: str(item).lower()):
        if not path.is_file() or "channels" in path.relative_to(root).parts:
            continue
        if path.suffix.lower() not in VIDEO_EXTENSIONS or ".part" in path.suffixes:
            continue
        name, confidence = canonical_name(path, root)
        destination = path.with_name(name)
        if destination == path:
            continue
        destination_key = str(destination).casefold()
        same_file = destination.exists() and destination.samefile(path)
        if destination_key in occupied or (destination.exists() and not same_file):
            raise FileExistsError(f"rename collision: {destination}")
        occupied.add(destination_key)
        renames.append(Rename(path, destination, confidence))
    return renames


def apply(renames: list[Rename], root: Path) -> Path:
    manifest = root / "rename-manifest.csv"
    with manifest.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(("confidence", "source", "destination"))
        writer.writerows((item.confidence, item.source, item.destination) for item in renames)
    staged = []
    for item in renames:
        temporary = item.source.with_name(f".{uuid.uuid4().hex}.rename")
        item.source.rename(temporary)
        staged.append((temporary, item.destination))
    for temporary, destination in staged:
        temporary.rename(destination)
    return manifest


def write_review(renames: list[Rename], root: Path) -> Path:
    review = root / "rename-review.csv"
    with review.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(("source", "suggested_destination"))
        writer.writerows((item.source, item.destination) for item in renames)
    return review


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", nargs="?", type=Path, default=Path("downloads"))
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    root = args.root.resolve()
    renames = build_plan(root)
    counts = {key: sum(item.confidence == key for item in renames) for key in ("high", "medium", "review")}
    print(f"planned={len(renames)} high={counts['high']} medium={counts['medium']} review={counts['review']}")
    if args.apply:
        approved = [item for item in renames if item.confidence != "review"]
        review = [item for item in renames if item.confidence == "review"]
        print(f"manifest={apply(approved, root)}")
        print(f"review={write_review(review, root)}")
    else:
        for item in renames[:40]: print(f"[{item.confidence}] {item.source.name} -> {item.destination.name}")
        print("Dry run only; pass --apply to rename.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
