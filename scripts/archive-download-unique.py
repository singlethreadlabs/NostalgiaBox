#!/usr/bin/env python3
"""Download one video file per episode from an Internet Archive item."""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

VIDEO_EXTENSIONS = {".avi", ".m4v", ".mkv", ".mov", ".mp4", ".webm"}
EPISODE_PATTERN = re.compile(
    r"(?i)(?P<prefix>.*?)(?:\bS(?P<season>\d{1,3})\s*E(?P<episode>\d{1,3})\b"
    r"|\b(?P<season_alt>\d{1,3})x(?P<episode_alt>\d{1,3})\b)"
)
USER_AGENT = "NostalgiaBox archive downloader/1.0"
CHUNK_SIZE = 4 * 1024 * 1024
RETRYABLE_HTTP_STATUS = {408, 425, 429, 500, 502, 503, 504}


class DownloadError(Exception):
    """A download could not be completed safely."""


class PermanentDownloadError(DownloadError):
    """A local file conflict requires user action before retrying."""


class ProgressTracker:
    """Track per-file byte positions safely across download threads."""

    def __init__(self) -> None:
        self._positions: dict[str, int] = {}
        self._lock = threading.Lock()

    def update(self, name: str, position: int) -> None:
        with self._lock:
            self._positions[name] = position

    def total(self) -> int:
        with self._lock:
            return sum(self._positions.values())


def item_identifier(value: str) -> str:
    """Accept either an Archive.org item identifier or item/download URL."""
    parsed = urlparse(value)
    if not parsed.scheme:
        return value.strip("/")

    if parsed.netloc.lower() not in {"archive.org", "www.archive.org"}:
        raise ValueError("URL must point to archive.org")

    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 2 or parts[0] not in {"details", "download"}:
        raise ValueError("expected an archive.org/details/... or /download/... URL")
    return parts[1]


def fetch_json(url: str) -> dict[str, Any]:
    request = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(request, timeout=60) as response:
        return json.load(response)


def episode_key(name: str) -> str:
    """Build a show-and-episode key that is stable across encode suffixes."""
    stem = Path(name).stem
    match = EPISODE_PATTERN.search(stem)
    if match:
        prefix = re.sub(r"[^a-z0-9]+", " ", match.group("prefix").lower()).strip()
        season = int(match.group("season") or match.group("season_alt"))
        episode = int(match.group("episode") or match.group("episode_alt"))
        return f"{prefix}|s{season:03d}e{episode:03d}"

    # Archive.org derivatives normally name their source in "original". This
    # fallback handles standalone videos without conventional episode numbers.
    return re.sub(r"[^a-z0-9]+", " ", stem.lower()).strip()


def video_files(metadata: dict[str, Any], match: str | None) -> list[dict[str, Any]]:
    pattern = re.compile(match, re.IGNORECASE) if match else None
    return [
        file
        for file in metadata.get("files", [])
        if Path(file.get("name", "")).suffix.lower() in VIDEO_EXTENSIONS
        and (pattern is None or pattern.search(file["name"]))
    ]


def movie_key(name: str) -> str:
    """Group alternate tape/disc captures of the same movie title."""
    stem = Path(name).stem
    stem = re.sub(r"(?i)\s*\(version\s+\d+\)\s*", " ", stem)
    stem = re.sub(
        r"(?i)\s*\((?:19|20)\d{2}\s+(?:vhs|dvd)(?:\s+[^)]*)?\)\s*",
        " ",
        stem,
    )
    return re.sub(r"[^a-z0-9]+", " ", stem.lower()).strip()


def preference(file: dict[str, Any], preferred_extension: str | None) -> tuple[int, ...]:
    extension = Path(file["name"]).suffix.lower().lstrip(".")
    width = int(file.get("width") or 0)
    height = int(file.get("height") or 0)
    size = int(file.get("size") or 0)
    return (
        int(extension == preferred_extension) if preferred_extension else 0,
        int(file.get("source") == "original"),
        width * height,
        size,
    )


def select_unique(
    files: list[dict[str, Any]],
    preferred_extension: str | None = None,
    *,
    group_movies: bool = False,
    prefer_smallest: bool = False,
) -> list[dict[str, Any]]:
    """Select the preferred encode for each show/season/episode."""
    selected: dict[str, dict[str, Any]] = {}
    for file in files:
        source_name = file.get("original") or file["name"]
        key = movie_key(source_name) if group_movies else episode_key(source_name)
        current = selected.get(key)
        file_preference = preference(file, preferred_extension)
        current_preference = (
            preference(current, preferred_extension) if current is not None else None
        )
        if prefer_smallest:
            file_preference = (-file_preference[-1],)
            if current_preference is not None:
                current_preference = (-current_preference[-1],)
        if current is None or file_preference > current_preference:
            selected[key] = file
    return sorted(selected.values(), key=lambda file: file["name"].lower())


def human_size(value: int) -> str:
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.1f} {unit}"
        size /= 1024
    raise AssertionError("unreachable")


def safe_destination(output: Path, archive_name: str) -> Path:
    relative = Path(archive_name)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"unsafe archive filename: {archive_name!r}")
    return output / relative


def download_once(
    url: str,
    destination: Path,
    expected_size: int,
    progress: Callable[[int], None] | None = None,
) -> str:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        actual_size = destination.stat().st_size
        if progress:
            progress(actual_size)
        if not expected_size or actual_size == expected_size:
            return f"skip (complete): {destination}"
        raise PermanentDownloadError(
            f"{destination} exists but is {actual_size} bytes; "
            f"expected {expected_size}. Move or remove it before retrying."
        )

    partial = destination.with_name(destination.name + ".part")
    offset = partial.stat().st_size if partial.exists() else 0
    if progress:
        progress(offset)
    if expected_size and offset == expected_size:
        partial.replace(destination)
        return f"recovered complete partial: {destination}"
    if expected_size and offset > expected_size:
        raise PermanentDownloadError(
            f"{partial} is {offset} bytes; expected at most {expected_size}"
        )

    headers = {"User-Agent": USER_AGENT}
    if offset:
        headers["Range"] = f"bytes={offset}-"
    request = Request(url, headers=headers)

    with urlopen(request, timeout=90) as response:
        status = getattr(response, "status", response.getcode())
        if offset and status == 206:
            mode = "ab"
        elif offset and status == 200:
            # Some mirrors ignore Range. Restart instead of corrupting the file
            # by appending a full response to an existing partial response.
            mode = "wb"
            offset = 0
        else:
            mode = "wb"

        with partial.open(mode) as output:
            while chunk := response.read(CHUNK_SIZE):
                output.write(chunk)
                if progress:
                    progress(output.tell())

    actual_size = partial.stat().st_size
    if expected_size and actual_size != expected_size:
        raise DownloadError(
            f"incomplete response for {destination.name}: "
            f"received {actual_size} of {expected_size} bytes"
        )
    partial.replace(destination)
    resumed = " (resumed)" if offset else ""
    return f"downloaded{resumed}: {destination}"


def download(
    url: str,
    destination: Path,
    expected_size: int,
    retries: int,
    progress: Callable[[int], None] | None = None,
) -> str:
    for attempt in range(retries + 1):
        try:
            return download_once(url, destination, expected_size, progress)
        except HTTPError as error:
            if error.code == 416:
                partial = destination.with_name(destination.name + ".part")
                if (
                    expected_size
                    and partial.exists()
                    and partial.stat().st_size == expected_size
                ):
                    partial.replace(destination)
                    return f"recovered complete partial: {destination}"
            if error.code not in RETRYABLE_HTTP_STATUS or attempt == retries:
                raise
            retry_after = error.headers.get("Retry-After")
            delay = (
                float(retry_after)
                if retry_after and retry_after.isdigit()
                else 2**attempt
            )
        except PermanentDownloadError:
            raise
        except (DownloadError, URLError, TimeoutError, OSError):
            if attempt == retries:
                raise
            delay = 2**attempt
        time.sleep(min(delay, 30))

    raise AssertionError("unreachable")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download exactly one preferred video encode per episode."
    )
    parser.add_argument("item", help="Archive.org item URL or identifier")
    parser.add_argument("-o", "--output", type=Path, default=Path("downloads"))
    parser.add_argument(
        "--match",
        metavar="REGEX",
        help="only include filenames matching this case-insensitive regular expression",
    )
    parser.add_argument(
        "--prefer",
        choices=sorted(extension.lstrip(".") for extension in VIDEO_EXTENSIONS),
        help="prefer this container when available (for a Pi, try: --prefer mp4)",
    )
    parser.add_argument(
        "--require",
        choices=sorted(extension.lstrip(".") for extension in VIDEO_EXTENSIONS),
        help="only select files in this container",
    )
    parser.add_argument(
        "--group-movies",
        action="store_true",
        help="deduplicate captures by movie title instead of episode number",
    )
    parser.add_argument(
        "--prefer-smallest",
        action="store_true",
        help="prefer the smallest candidate within each duplicate group",
    )
    parser.add_argument(
        "--max-file-mib",
        type=float,
        help="exclude individual files larger than this many MiB",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="list selections without downloading"
    )
    parser.add_argument(
        "--jobs",
        type=int,
        default=4,
        help="simultaneous downloads (default: 4; try 6-8 on a fast connection)",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=5,
        help="retries per file after transient failures (default: 5)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.jobs < 1:
        print("error: --jobs must be at least 1", file=sys.stderr)
        return 2
    if args.retries < 0:
        print("error: --retries cannot be negative", file=sys.stderr)
        return 2

    try:
        identifier = item_identifier(args.item)
        metadata = fetch_json(
            f"https://archive.org/metadata/{quote(identifier, safe='')}"
        )
        candidates = video_files(metadata, args.match)
        if args.require:
            candidates = [
                file
                for file in candidates
                if Path(file["name"]).suffix.lower() == f".{args.require}"
            ]
        if args.max_file_mib is not None:
            if args.max_file_mib <= 0:
                raise ValueError("--max-file-mib must be greater than zero")
            maximum_bytes = int(args.max_file_mib * 1024 * 1024)
            candidates = [
                file
                for file in candidates
                if int(file.get("size") or 0) <= maximum_bytes
            ]
        selected = select_unique(
            candidates,
            args.prefer,
            group_movies=args.group_movies,
            prefer_smallest=args.prefer_smallest,
        )
        destinations = {
            file["name"]: safe_destination(args.output, file["name"])
            for file in selected
        }
    except (ValueError, re.error, HTTPError, URLError, TimeoutError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    if not selected:
        print("error: no matching video files found", file=sys.stderr)
        return 1

    total = sum(int(file.get("size") or 0) for file in selected)
    print(
        f"Selected {len(selected)} unique episodes from {len(candidates)} video files "
        f"({human_size(total)} total)."
    )
    for file in selected:
        print(f"{human_size(int(file.get('size') or 0)):>10}  {file['name']}")

    if args.dry_run:
        return 0

    base_url = f"https://archive.org/download/{quote(identifier, safe='')}/"
    failures: list[tuple[str, Exception]] = []
    tracker = ProgressTracker()
    for file in selected:
        destination = destinations[file["name"]]
        partial = destination.with_name(destination.name + ".part")
        existing = destination if destination.exists() else partial
        tracker.update(
            file["name"], existing.stat().st_size if existing.exists() else 0
        )

    print(
        f"Starting downloads with {args.jobs} workers. "
        "Interrupted .part files resume on the next run."
    )
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as executor:
        futures = {
            executor.submit(
                download,
                base_url + quote(file["name"], safe="/"),
                destinations[file["name"]],
                int(file.get("size") or 0),
                args.retries,
                lambda position, name=file["name"]: tracker.update(name, position),
            ): file["name"]
            for file in selected
        }
        pending = set(futures)
        completed = 0
        previous_bytes = tracker.total()
        previous_time = time.monotonic()
        while pending:
            done, pending = concurrent.futures.wait(
                pending,
                timeout=1,
                return_when=concurrent.futures.FIRST_COMPLETED,
            )
            now = time.monotonic()
            current_bytes = tracker.total()
            elapsed = max(now - previous_time, 0.001)
            speed = max(current_bytes - previous_bytes, 0) / elapsed
            percentage = current_bytes / total * 100 if total else 0
            status = (
                f"{completed}/{len(selected)} files | "
                f"{human_size(current_bytes)} / {human_size(total)} | "
                f"{percentage:5.1f}% | {human_size(int(speed))}/s"
            )

            if not done:
                print(f"\r{status:<100}", end="", flush=True)
                previous_bytes = current_bytes
                previous_time = now
                continue

            print("\r" + " " * 100 + "\r", end="", flush=True)
            for future in done:
                completed += 1
                name = futures[future]
                try:
                    result = future.result()
                    print(f"[{completed}/{len(selected)}] {result}")
                except Exception as error:
                    failures.append((name, error))
                    print(
                        f"[{completed}/{len(selected)}] failed: {name}: {error}",
                        file=sys.stderr,
                    )
            previous_bytes = tracker.total()
            previous_time = time.monotonic()

    if failures:
        print(
            f"\n{len(failures)} download(s) failed. Run the same command again "
            "to resume partial files:",
            file=sys.stderr,
        )
        for name, error in failures:
            print(f"  {name}: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
