#!/usr/bin/env python3
"""Build a deterministic, collision-safe SSD migration manifest."""

from __future__ import annotations

import argparse
import csv
import re
from dataclasses import dataclass
from pathlib import Path


VIDEO_EXTENSIONS = {".avi", ".m4v", ".mkv", ".mov", ".mp4", ".webm"}
INVALID_EXFAT = re.compile(r'["*/:<>?\\|\x00-\x1f]')
QUALITY_SUFFIX = re.compile(
    r"(?:[ ._-]+(?:360|480|576|720|1080|2160)p\b.*|"
    r"\s*\((?:360|480|576|720|1080|2160)p\s*[- ].*\)\s*)$",
    re.IGNORECASE,
)
DBZ_SEASONS = (39, 35, 33, 32, 26, 29, 25, 34, 38)


@dataclass(frozen=True)
class Entry:
    source: Path
    destination: Path
    size: int
    confidence: str
    reason: str


def safe_component(value: str) -> str:
    value = INVALID_EXFAT.sub(" - ", value)
    value = re.sub(r"\s+", " ", value).strip(" .-")
    return value or "Untitled"


def clean_title(value: str) -> str:
    value = re.sub(r"[.]ia$", "", value, flags=re.I)
    value = value.replace("_", " ")
    if "." in value and " " not in value:
        value = value.replace(".", " ")
    value = QUALITY_SUFFIX.sub("", value)
    value = re.sub(r"\s*\((?:Times Forgotten|mbaldw)\)\s*$", "", value, flags=re.I)
    value = re.sub(r"\s*\((?:1080p|720p|480p)[^)]*\)\s*$", "", value, flags=re.I)
    value = re.sub(r"^[\s._-]+|[\s._-]+$", "", value)
    return safe_component(value)


def dbz_season_episode(absolute: int) -> tuple[int, int]:
    remaining = absolute
    for season, count in enumerate(DBZ_SEASONS, 1):
        if remaining <= count:
            return season, remaining
        remaining -= count
    raise ValueError(f"Dragon Ball Z episode out of range: {absolute}")


def parse_episode(path: Path, show: str) -> tuple[int, int, str, str] | None:
    stem = path.stem

    canonical = re.match(
        rf"^{re.escape(show)}\s+-\s+S(\d{{1,2}})E(\d{{1,3}})(?:\s+-\s+(.+))?$",
        stem,
        re.I,
    )
    if canonical:
        return int(canonical.group(1)), int(canonical.group(2)), clean_title(canonical.group(3) or ""), "source episode code"

    spaced = re.search(r"S(\d{1,2})\s*E(\d{1,3})(?:[ ._-]+(.*))?$", stem, re.I)
    if spaced:
        return int(spaced.group(1)), int(spaced.group(2)), clean_title(spaced.group(3) or ""), "source episode code"

    xcode = re.search(r"(?<!\d)(\d{1,2})x(\d{1,3})(?:\s*[- ]\s*(.*))?$", stem, re.I)
    if xcode:
        return int(xcode.group(1)), int(xcode.group(2)), clean_title(xcode.group(3) or ""), "source episode code"

    comma = re.search(r"(?:^|\s)(\d{1,2}),(\d{1,3})\s*[- ]\s*(.*)$", stem)
    if comma:
        return int(comma.group(1)), int(comma.group(2)), clean_title(comma.group(3)), "source episode code"

    if show == "Dragon Ball Z (1989)":
        match = re.search(r"Dragon Ball Z[ .](\d{3})\b", stem, re.I)
        if match:
            season, episode = dbz_season_episode(int(match.group(1)))
            return season, episode, "", "absolute episode mapped to nine-season order"

    return None


def canonical_episode_path(
    show: str, season: int, episode: int, title: str, suffix: str
) -> Path:
    safe_show = safe_component(show)
    filename = f"{safe_show} - S{season:02d}E{episode:02d}"
    if title:
        filename += f" - {safe_component(title)}"
    return Path("Shows") / safe_show / f"Season {season:02d}" / f"{filename}{suffix.lower()}"


def natural_key(path: Path) -> tuple[object, ...]:
    return tuple(int(part) if part.isdigit() else part.casefold() for part in re.split(r"(\d+)", str(path)))


def special_title(path: Path, show: str) -> str:
    stem = path.stem
    show_title = re.sub(r"\s+\(\d{4}\)$", "", show)
    stem = re.sub(rf"^{re.escape(show_title)}\s*[- ]*", "", stem, flags=re.I)
    return clean_title(stem)


def show_entries(root: Path) -> list[Entry]:
    entries: list[Entry] = []
    shows_root = root / "Shows"
    for show_dir in sorted((p for p in shows_root.iterdir() if p.is_dir()), key=natural_key):
        show = show_dir.name
        files = sorted(
            (p for p in show_dir.rglob("*") if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS),
            key=natural_key,
        )
        inferred_by_season: dict[int, list[Path]] = {}
        unnumbered: list[Path] = []
        for source in files:
            parsed = parse_episode(source, show)
            if parsed:
                season, episode, title, reason = parsed
                destination = canonical_episode_path(show, season, episode, title, source.suffix)
                entries.append(Entry(source, destination, source.stat().st_size, "high", reason))
                continue
            season_folder = next(
                (part for part in source.relative_to(show_dir).parts[:-1] if re.fullmatch(r"Season \d{1,2}", part, re.I)),
                None,
            )
            if show == "CatDog (1998)" and season_folder:
                inferred_by_season.setdefault(int(re.search(r"\d+", season_folder).group()), []).append(source)
            else:
                unnumbered.append(source)

        for season, sources in inferred_by_season.items():
            for episode, source in enumerate(sorted(sources, key=natural_key), 1):
                destination = canonical_episode_path(show, season, episode, "", source.suffix)
                entries.append(Entry(source, destination, source.stat().st_size, "inferred", "ordered from CatDog season disc export"))

        default_season = 1 if show == "Amazing Animals (1996)" else 0
        for episode, source in enumerate(sorted(unnumbered, key=natural_key), 1):
            title = special_title(source, show)
            destination = canonical_episode_path(show, default_season, episode, title, source.suffix)
            entries.append(Entry(source, destination, source.stat().st_size, "inferred", "stable title order; source has no episode code"))
    return entries


DCOM_YEARS = {
    "Brink!": 1998,
    "Halloweentown": 1998,
    "Halloweentown II Kalabars Revenge": 2001,
    "Halloweentown High": 2004,
    "Return to Halloweentown": 2006,
    "Twitches": 2005,
    "Wendy Wu Homecoming Warrior": 2006,
}


def movie_identity(source: Path, collection: str) -> tuple[str, str, str]:
    stem = source.stem
    stem = re.sub(r"\.ia$", "", stem, flags=re.I)
    stem = re.sub(r"^\d+\.\s*", "", stem)
    if collection == "Disney VHS Collection":
        match = re.match(r"(.+?)\s*\((\d{4}) VHS\)", stem, re.I)
        if not match:
            raise ValueError(f"unrecognized VHS movie: {source}")
        return clean_title(match.group(1)), f"VHS {match.group(2)}", "source VHS year"

    stem = re.sub(r"\s+Disney\s+\(DCOM#\d+\).*", "", stem, flags=re.I)
    stem = re.sub(r"[.]", " ", stem)
    stem = re.sub(r"\s+", " ", stem).strip()
    match = re.match(r"(.+?)\s+(\d{4})$", stem)
    if not match:
        match = re.match(r"(.+?)\s*\((\d{4})\)$", stem)
    if match:
        return clean_title(match.group(1)), match.group(2), "source release year"
    title = clean_title(stem)
    if title not in DCOM_YEARS:
        raise ValueError(f"unrecognized DCOM year: {source}")
    return title, str(DCOM_YEARS[title]), "reviewed DCOM release year"


def movie_entries(root: Path) -> list[Entry]:
    entries: list[Entry] = []
    movies_root = root / "Movies"
    if not movies_root.exists():
        return entries
    for collection_dir in sorted((p for p in movies_root.iterdir() if p.is_dir()), key=natural_key):
        collection = safe_component(collection_dir.name)
        for source in sorted(
            (p for p in collection_dir.rglob("*") if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS),
            key=natural_key,
        ):
            title, qualifier, reason = movie_identity(source, collection_dir.name)
            identity = safe_component(f"{title} ({qualifier})")
            destination = Path("Movies") / collection / identity / f"{identity}{source.suffix.lower()}"
            entries.append(Entry(source, destination, source.stat().st_size, "high", reason))
    return entries


def passthrough_entries(root: Path) -> list[Entry]:
    entries: list[Entry] = []
    for top in ("Extras", "Archives", "Broadcasts"):
        base = root / top
        if not base.exists():
            continue
        for source in sorted((p for p in base.rglob("*") if p.is_file()), key=natural_key):
            relative = source.relative_to(root)
            destination = Path(*(safe_component(part) for part in relative.parts))
            entries.append(Entry(source, destination, source.stat().st_size, "high", "passthrough asset"))
    return entries


def build_manifest(root: Path) -> list[Entry]:
    entries = show_entries(root) + movie_entries(root) + passthrough_entries(root)
    destinations: dict[str, Path] = {}
    for entry in entries:
        key = str(entry.destination).casefold()
        if key in destinations:
            raise FileExistsError(f"destination collision: {destinations[key]} and {entry.source} -> {entry.destination}")
        destinations[key] = entry.source
    return sorted(entries, key=lambda item: natural_key(item.destination))


def write_manifest(entries: list[Entry], root: Path, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(("source", "destination", "size", "confidence", "reason"))
        writer.writerows(
            (
                entry.source.relative_to(root),
                entry.destination,
                entry.size,
                entry.confidence,
                entry.reason,
            )
            for entry in entries
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    entries = build_manifest(root)
    write_manifest(entries, root, args.output.resolve())
    changed = sum(entry.source.relative_to(root) != entry.destination for entry in entries)
    inferred = sum(entry.confidence == "inferred" for entry in entries)
    print(f"files={len(entries)} changed={changed} inferred={inferred} bytes={sum(entry.size for entry in entries)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
