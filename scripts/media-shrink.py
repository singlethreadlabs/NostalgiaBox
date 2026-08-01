#!/usr/bin/env python3
"""Stage smaller, verified MP4 copies using HandBrake without touching sources."""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import shutil
import subprocess
import sys
import threading
from dataclasses import asdict, dataclass
from pathlib import Path


MEDIA_SUFFIXES = {".m4v", ".mkv", ".mov", ".mp4", ".webm"}
MANIFEST_LOCK = threading.Lock()


@dataclass(frozen=True)
class Result:
    source: str
    output: str | None
    status: str
    source_bytes: int
    output_bytes: int
    savings_bytes: int
    duration: float


def run(command: list[str], description: str) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    if completed.returncode:
        detail = completed.stderr.strip().splitlines()
        raise RuntimeError(f"{description}: {detail[-1] if detail else 'failed'}")
    return completed


def probe_duration(path: Path) -> float:
    completed = run(
        [
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", str(path),
        ],
        f"probe {path}",
    )
    return float(completed.stdout.strip())


def find_media(root: Path, output_dir: Path) -> list[Path]:
    resolved_output = output_dir.resolve()
    # Channel folders contain symlinks to canonical episodes. Resolve and deduplicate
    # them so the same source is never encoded concurrently or staged twice.
    return sorted({
        path.resolve()
        for path in root.rglob("*")
        if path.is_file()
        and path.suffix.lower() in MEDIA_SUFFIXES
        and not path.resolve().is_relative_to(resolved_output)
    })


def write_manifest(path: Path, results: list[Result], failures: list[dict[str, str]]) -> None:
    payload = {
        "status": "staged-for-review",
        "replace_sources": False,
        "files": [asdict(result) for result in sorted(results, key=lambda item: item.source)],
        "failures": sorted(failures, key=lambda item: item["source"]),
    }
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def shrink_one(
    source: Path,
    destination: Path,
    *,
    preset: str,
    quality: int,
    audio_bitrate: int,
) -> Result:
    destination.parent.mkdir(parents=True, exist_ok=True)
    handbrake_output = destination.with_suffix(".video.part.mp4")
    final_output = destination.with_suffix(".part.mp4")
    handbrake_output.unlink(missing_ok=True)
    final_output.unlink(missing_ok=True)
    source_bytes = source.stat().st_size
    source_duration = probe_duration(source)

    try:
        run(
            [
                "HandBrakeCLI", "-i", str(source), "-o", str(handbrake_output),
                "-Z", preset, "-q", str(quality), "-E", "copy", "--vfr", "-O",
            ],
            f"encode video for {source}",
        )
        run(
            [
                "ffmpeg", "-nostdin", "-hide_banner", "-v", "error",
                "-i", str(handbrake_output), "-map", "0:v:0", "-map", "0:a?",
                "-map", "0:s?", "-c:v", "copy", "-c:a", "aac", "-b:a",
                f"{audio_bitrate}k", "-c:s", "copy", "-map_metadata", "0",
                "-movflags", "+faststart", "-y", str(final_output),
            ],
            f"encode audio for {source}",
        )
        output_duration = probe_duration(final_output)
        tolerance = max(1.0, source_duration * 0.01)
        if abs(output_duration - source_duration) > tolerance:
            raise RuntimeError(
                f"duration mismatch for {source}: {source_duration:.3f}s source, "
                f"{output_duration:.3f}s output"
            )
        run(
            [
                "ffmpeg", "-nostdin", "-hide_banner", "-v", "error",
                "-i", str(final_output), "-f", "null", "-",
            ],
            f"decode verification for {source}",
        )
        output_bytes = final_output.stat().st_size
        if output_bytes >= source_bytes:
            return Result(
                str(source), None, "kept-source-smaller", source_bytes,
                output_bytes, 0, source_duration,
            )
        final_output.replace(destination)
        return Result(
            str(source), str(destination), "verified", source_bytes,
            output_bytes, source_bytes - output_bytes, output_duration,
        )
    finally:
        handbrake_output.unlink(missing_ok=True)
        final_output.unlink(missing_ok=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--preset", default="Fast 480p30")
    parser.add_argument("--quality", type=int, default=22)
    parser.add_argument("--audio-bitrate", type=int, default=96)
    parser.add_argument("--jobs", type=int, default=6)
    parser.add_argument("--resume", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    for command in ("HandBrakeCLI", "ffmpeg", "ffprobe"):
        if shutil.which(command) is None:
            print(f"error: {command} is required", file=sys.stderr)
            return 2
    if not args.root.is_dir() or not 0 <= args.quality <= 51 or args.jobs < 1:
        print("error: root must be a directory, quality must be 0-51, and jobs >= 1", file=sys.stderr)
        return 2

    args.output_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = args.output_dir / "manifest.json"
    if any(args.output_dir.iterdir()) and not args.resume:
        print("error: output directory must be empty (or use --resume)", file=sys.stderr)
        return 1

    results: list[Result] = []
    failures: list[dict[str, str]] = []
    if args.resume and manifest_path.exists():
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        results = [Result(**item) for item in payload.get("files", [])]
    completed = {item.source for item in results}
    sources = find_media(args.root, args.output_dir)
    pending = [source for source in sources if str(source) not in completed]
    print(f"Found {len(sources)} files; {len(pending)} pending; {args.jobs} parallel jobs.", flush=True)

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as executor:
        futures = {}
        for source in pending:
            relative = source.relative_to(args.root.resolve()).with_suffix(".mp4")
            futures[executor.submit(
                shrink_one, source, args.output_dir / relative,
                preset=args.preset, quality=args.quality,
                audio_bitrate=args.audio_bitrate,
            )] = source
        for index, future in enumerate(concurrent.futures.as_completed(futures), start=len(completed) + 1):
            source = futures[future]
            try:
                result = future.result()
                results.append(result)
                saved = result.savings_bytes / (1024 ** 2)
                print(f"[{index}/{len(sources)}] {result.status} {source.name} ({saved:.1f} MiB saved)", flush=True)
            except (OSError, RuntimeError, ValueError) as error:
                failures.append({"source": str(source), "error": str(error)})
                print(f"[{index}/{len(sources)}] FAILED {source}: {error}", file=sys.stderr, flush=True)
            with MANIFEST_LOCK:
                write_manifest(manifest_path, results, failures)

    write_manifest(manifest_path, results, failures)
    print(f"Complete: {len(results)} processed, {len(failures)} failed.", flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
