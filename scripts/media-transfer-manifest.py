#!/usr/bin/env python3
"""Generate a byte-verifiable media inventory for homelab transfers."""

from __future__ import annotations

import argparse
import concurrent.futures
import csv
import hashlib
import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path


VIDEO_EXTENSIONS = {".avi", ".m4v", ".mkv", ".mov", ".mp4", ".webm"}


@dataclass(frozen=True)
class Entry:
    path: str
    size: int
    sha256: str
    duration: str
    video_codec: str
    width: str
    height: str
    audio_codec: str


def inspect(path: Path, root: Path) -> Entry:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(8 * 1024 * 1024):
            digest.update(chunk)
    completed = subprocess.run(
        [
            "ffprobe", "-v", "error", "-show_streams", "-show_format",
            "-of", "json", str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode:
        raise RuntimeError(f"ffprobe failed for {path}: {completed.stderr.strip()}")
    payload = json.loads(completed.stdout)
    streams = payload.get("streams", [])
    video = next((item for item in streams if item.get("codec_type") == "video"), {})
    audio = next((item for item in streams if item.get("codec_type") == "audio"), {})
    return Entry(
        path=path.relative_to(root).as_posix(),
        size=path.stat().st_size,
        sha256=digest.hexdigest(),
        duration=str(payload.get("format", {}).get("duration", "")),
        video_codec=str(video.get("codec_name", "")),
        width=str(video.get("width", "")),
        height=str(video.get("height", "")),
        audio_codec=str(audio.get("codec_name", "")),
    )


def write_checksums(entries: list[Entry], output: Path) -> None:
    temporary = output.with_name(f".{output.name}.tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        for entry in entries:
            stream.write(f"{entry.sha256}  {entry.path}\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, output)


def generate(root: Path, output: Path, checksums: Path, jobs: int) -> list[Entry]:
    files = sorted(
        path for path in root.rglob("*")
        if path.is_file() and not path.is_symlink()
        and path.suffix.lower() in VIDEO_EXTENSIONS
    )
    entries: list[Entry] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as executor:
        futures = {executor.submit(inspect, path, root): path for path in files}
        for count, future in enumerate(concurrent.futures.as_completed(futures), 1):
            entries.append(future.result())
            if count % 100 == 0 or count == len(files):
                print(f"Hashed and probed {count}/{len(files)}", flush=True)
    entries.sort(key=lambda entry: entry.path.casefold())
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.tmp")
    with temporary.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=Entry.__dataclass_fields__)
        writer.writeheader()
        writer.writerows(entry.__dict__ for entry in entries)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, output)
    write_checksums(entries, checksums)
    return entries


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", nargs="?", type=Path, default=Path("media"))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--checksums", type=Path)
    parser.add_argument("--jobs", type=int, default=4)
    args = parser.parse_args()
    root = args.root.resolve()
    output = (args.output or root / "library-manifest.csv").resolve()
    checksums = (args.checksums or root / "SHA256SUMS").resolve()
    if not root.is_dir() or args.jobs < 1:
        print("error: root must exist and jobs must be positive")
        return 2
    entries = generate(root, output, checksums, args.jobs)
    print(f"Wrote {len(entries)} entries to {output}")
    print(f"Wrote transfer checksums to {checksums}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
