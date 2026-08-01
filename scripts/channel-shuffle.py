#!/usr/bin/env python3
"""Build a non-destructive, theme-based channel library from downloads.

The generated tree contains relative symlinks only. Source media is never moved,
renamed, or copied, and rerunning the command reconciles links it owns.
"""

from __future__ import annotations

import argparse
import csv
import os
import re
from dataclasses import dataclass
from pathlib import Path


VIDEO_EXTENSIONS = {".avi", ".m4v", ".mkv", ".mov", ".mp4", ".webm"}


@dataclass(frozen=True)
class Show:
    name: str
    source: str
    pattern: str | None = None

    def matches(self, path: Path, downloads: Path) -> bool:
        try:
            relative = path.relative_to(downloads / self.source)
        except ValueError:
            return False
        return self.pattern is None or re.search(self.pattern, relative.name, re.I) is not None


SHOWS = {
    "allegra": Show("Allegra's Window", "nick-jr", r"allegra|^season\s+[123]\s+episode"),
    "american-dragon": Show("American Dragon", "disney-channel", r"^American Dragon"),
    "arthur": Show("Arthur", "arthur"),
    "blues-clues": Show(
        "Blue's Clues and Blue's Room",
        "nick-jr",
        r"blue.?s clues|blue.?s room|^blue(?:s|’s|'s)?\b|^BC(?:BR|Snack)",
    ),
    "dragon-tales": Show("Dragon Tales", "dragon-tales"),
    "even-stevens": Show("Even Stevens", "disney-channel", r"^Even Stevens"),
    "gullah-gullah": Show("Gullah Gullah Island", "nick-jr", r"gullah"),
    "hannah-montana": Show("Hannah Montana", "disney-channel", r"^Hannah[ .]Montana"),
    "kablam": Show("KaBlam!", "kablam"),
    "kim-possible": Show("Kim Possible", "disney-channel", r"^Kim Possible"),
    "lilo-and-stitch": Show("Lilo & Stitch", "disney-channel", r"^Lilo"),
    "little-bill": Show("Little Bill", "nick-jr", r"little bill|baby in the ring"),
    "lizzie-mcguire": Show("Lizzie McGuire", "disney-channel", r"^Lizzie"),
    "magic-school-bus": Show("The Magic School Bus", "magic-schoolbus"),
    "phil-of-the-future": Show("Phil of the Future", "disney-channel", r"^Phil of the Future"),
    "proud-family": Show("The Proud Family", "disney-channel", r"^The Proud Family"),
    "recess": Show("Recess", "disney-channel", r"^Recess"),
    "suite-life": Show("The Suite Life", "disney-channel", r"^The Suite Life"),
    "thats-so-raven": Show("That's So Raven", "disney-channel", r"^That.?s So Raven"),
    "winnie-the-pooh": Show(
        "The New Adventures of Winnie the Pooh",
        "disney-channel",
        r"^The.New.Adventures.of.Winnie.the.Pooh",
    ),
    "wizards": Show("Wizards of Waverly Place", "disney-channel", r"^Wizards of Waverly Place"),
    "wonder-pets": Show("The Wonder Pets", "nick-jr", r"wonder pets|^save the"),
    "courage": Show("Courage the Cowardly Dog", "courage-the-cowardly-dog"),
    "dexters-lab": Show("Dexter's Laboratory", "dexters-laboratory"),
    "dragon-ball": Show(
        "Dragon Ball",
        "Cartoon Network Consolidated/Series/Dragon Ball/Toonami Edit",
    ),
    "dragon-ball-z": Show(
        "Dragon Ball Z",
        "Cartoon Network Consolidated/Series/Dragon Ball Z/Toonami Edit",
    ),
    "fairly-oddparents": Show(
        "The Fairly OddParents",
        "the-fairly-oddparents-the-complete-series_202507",
    ),
    "iron-man": Show("Iron Man (1994)", "iron-man-1994"),
    "pokemon": Show("Pokémon", "pokemon"),
    "sonic-x": Show("Sonic X", "sonic-x"),
    "super-mario-world": Show("Super Mario World", "super-mario-world"),
    "the-legend-of-zelda": Show("The Legend of Zelda", "legend-of-zelda"),
    "tmnt-2003": Show("Teenage Mutant Ninja Turtles (2003)", "tmnt-2003"),
    "toonami-blocks": Show(
        "Toonami Broadcast Blocks",
        "Cartoon Network Consolidated/Broadcast Blocks",
    ),
    "x-men": Show("X-Men", "x-men"),
    "nick-jr-broadcasts": Show(
        "Nick Jr. Broadcast Recordings",
        "nick-jr",
        r"tape|recording|marathon|compilation|vhs|airing|playdate|mega music fest|dvd-r|disney and nick",
    ),
}


CHANNELS = {
    "02-slime-time-rewind": ("courage", "fairly-oddparents", "kablam"),
    "03-orbit-2000": ("lilo-and-stitch", "proud-family", "sonic-x"),
    "04-little-sprout-playhouse": (
        "allegra", "blues-clues", "dragon-tales", "little-bill", "wonder-pets"
    ),
    "05-bright-minds-tv": ("arthur", "gullah-gullah", "magic-school-bus"),
    "06-cozy-corner": ("allegra", "dragon-tales", "little-bill", "winnie-the-pooh"),
    "07-cartoon-lab": ("courage", "dexters-lab", "fairly-oddparents", "kablam"),
    "08-city-toons": ("american-dragon", "kim-possible", "proud-family", "tmnt-2003"),
    "09-nova-action": (
        "dragon-ball", "dragon-ball-z", "pokemon", "sonic-x", "toonami-blocks",
    ),
    "10-wonder-channel": (
        "dragon-tales", "lilo-and-stitch", "super-mario-world", "the-legend-of-zelda",
        "winnie-the-pooh", "wizards",
    ),
    "11-studio-live": (
        "even-stevens", "hannah-montana", "lizzie-mcguire", "phil-of-the-future",
        "suite-life", "thats-so-raven", "wizards",
    ),
    "12-saturday-signal": (
        "courage", "dexters-lab", "iron-man", "kablam",
        "recess", "super-mario-world", "the-legend-of-zelda", "x-men",
    ),
    "13-powerhouse-kids": (
        "american-dragon", "iron-man", "kim-possible", "tmnt-2003", "x-men",
    ),
    "14-sick-day-tv": (
        "arthur", "blues-clues", "dragon-tales",
        "nick-jr-broadcasts", "winnie-the-pooh",
    ),
    "15-saturday-club": (
        "american-dragon", "arthur", "dragon-tales", "kablam", "kim-possible",
        "courage", "dexters-lab", "fairly-oddparents",
        "lilo-and-stitch", "magic-school-bus", "pokemon", "proud-family", "recess",
        "sonic-x", "super-mario-world", "the-legend-of-zelda", "tmnt-2003", "x-men",
    ),
}

NICK_ASSET_CHANNELS = (
    "04-little-sprout-playhouse",
    "05-bright-minds-tv",
    "06-cozy-corner",
    "14-sick-day-tv",
    "15-saturday-club",
)
BUMPER_PATTERN = re.compile(
    r"promo|preview|commercial|sign.?off|home video|productions? logo|\bintro\b|\boutro\b|\bid\b",
    re.I,
)


def media_files(downloads: Path) -> list[Path]:
    return sorted(
        path
        for path in downloads.rglob("*")
        if path.is_file()
        and path.suffix.lower() in VIDEO_EXTENSIONS
        and ".part" not in path.suffixes
        and "channels" not in path.relative_to(downloads).parts
    )


def classify(files: list[Path], downloads: Path) -> list[dict[str, str]]:
    channel_membership = {
        key: [channel for channel, keys in CHANNELS.items() if key in keys]
        for key in SHOWS
    }
    rows = []
    for path in files:
        relative = path.relative_to(downloads)
        if "_Review" in relative.parts and "Exact Duplicates" in relative.parts:
            rows.append({"path": str(relative), "show": "", "kind": "excluded", "channels": "", "reason": "hash-confirmed duplicate"})
            continue
        matches = [key for key, show in SHOWS.items() if show.matches(path, downloads)]
        if matches:
            key = matches[0]
            kind = "broadcast" if key in {"toonami-blocks", "nick-jr-broadcasts"} else "show"
            rows.append({"path": str(relative), "show": SHOWS[key].name, "kind": kind, "channels": ";".join(channel_membership[key]), "reason": "matched canonical show rule"})
        elif relative.parts[0] == "nick-jr" and BUMPER_PATTERN.search(path.name):
            rows.append({"path": str(relative), "show": "Nick Jr. Archive", "kind": "bumper", "channels": ";".join(NICK_ASSET_CHANNELS), "reason": "matched promo/interstitial rule"})
        else:
            rows.append({"path": str(relative), "show": "", "kind": "review", "channels": "", "reason": "insufficient filename evidence"})
    return rows


def desired_links(downloads: Path, destination: Path, files: list[Path]) -> tuple[dict[Path, str], dict[str, int]]:
    desired: dict[Path, str] = {}
    counts: dict[str, int] = {}
    canonical_show = {
        path: next((key for key, show in SHOWS.items() if show.matches(path, downloads)), None)
        for path in files
    }
    for channel, show_keys in CHANNELS.items():
        channel_count = 0
        for show_key in show_keys:
            show = SHOWS[show_key]
            matches = [
                path
                for path in files
                if canonical_show[path] == show_key
                and "_Review" not in path.relative_to(downloads).parts
            ]
            for source in matches:
                link = destination / channel / show.name / source.name
                if link in desired:
                    raise FileExistsError(f"destination collision: {link}")
                desired[link] = os.path.relpath(source, link.parent)
            channel_count += len(matches)
        counts[channel] = channel_count
    rows = classify(files, downloads)
    for row in rows:
        if row["kind"] != "bumper":
            continue
        source = downloads / row["path"]
        for channel in NICK_ASSET_CHANNELS:
            link = destination / channel / "_Bumpers" / "Nick Jr. Archive" / source.name
            if link in desired:
                raise FileExistsError(f"destination collision: {link}")
            desired[link] = os.path.relpath(source, link.parent)
    return desired, counts


def build(downloads: Path, destination: Path, *, dry_run: bool = False) -> tuple[int, dict[str, int]]:
    files = media_files(downloads)
    desired, counts = desired_links(downloads, destination, files)
    existing = {path: os.readlink(path) for path in destination.rglob("*") if path.is_symlink()} if destination.exists() else {}
    unexpected = [path for path in destination.rglob("*") if path.is_file() and not path.is_symlink()] if destination.exists() else []
    if unexpected:
        raise ValueError(f"unexpected regular file in generated tree: {unexpected[0]}")
    remove = set(existing) - set(desired)
    replace = {path for path in set(existing) & set(desired) if existing[path] != desired[path]}
    add = set(desired) - set(existing)
    if not dry_run:
        for path in sorted(remove | replace):
            path.unlink()
        for path in sorted(add | replace):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.symlink_to(desired[path])
        if destination.exists():
            for directory in sorted((path for path in destination.rglob("*") if path.is_dir()), key=lambda path: len(path.parts), reverse=True):
                if not any(directory.iterdir()):
                    directory.rmdir()
    return len(add | replace), counts


def write_coverage(rows: list[dict[str, str]], path: Path) -> None:
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=("path", "show", "kind", "channels", "reason"))
        writer.writeheader()
        writer.writerows(rows)


def reconciliation_counts(downloads: Path, destination: Path) -> tuple[int, int, int]:
    desired, _ = desired_links(downloads, destination, media_files(downloads))
    existing = {path: os.readlink(path) for path in destination.rglob("*") if path.is_symlink()} if destination.exists() else {}
    remove = len(set(existing) - set(desired))
    replace = sum(existing[path] != desired[path] for path in set(existing) & set(desired))
    add = len(set(desired) - set(existing))
    return add, replace, remove


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("downloads", nargs="?", type=Path, default=Path("downloads"))
    parser.add_argument("--destination", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--coverage", type=Path)
    args = parser.parse_args()

    downloads = args.downloads.resolve()
    destination = (args.destination or downloads / "channels").resolve()
    files = media_files(downloads)
    rows = classify(files, downloads)
    additions, replacements, removals = reconciliation_counts(downloads, destination)
    linked, counts = build(downloads, destination, dry_run=args.dry_run)
    for channel, count in counts.items():
        print(f"{channel}: {count} playable files")
    coverage = args.coverage or downloads / "channel-coverage.csv"
    if not args.dry_run:
        write_coverage(rows, coverage)
    kind_counts = {kind: sum(row["kind"] == kind for row in rows) for kind in ("show", "broadcast", "bumper", "excluded", "review")}
    print("Coverage: " + ", ".join(f"{kind}={count}" for kind, count in kind_counts.items()))
    verb = "Would reconcile" if args.dry_run else "Reconciled"
    print(f"{verb} links: add={additions}, replace={replacements}, remove={removals}")
    print(f"Generated tree: {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
