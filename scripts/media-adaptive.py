#!/usr/bin/env python3
"""Adaptively shrink worthwhile media files and atomically replace verified sources."""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import shutil
import subprocess
import sys
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


MEDIA_SUFFIXES = {".m4v", ".mkv", ".mov", ".mp4", ".webm"}
JOURNAL_LOCK = threading.Lock()


@dataclass(frozen=True)
class Media:
    path: Path
    size: int
    duration: float
    width: int
    height: int
    bitrate: int
    audio_streams: int
    subtitle_streams: int


@dataclass(frozen=True)
class Result:
    source: str
    status: str
    source_bytes: int
    output_bytes: int
    savings_bytes: int
    duration: float


def run(command: list[str], description: str) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    if completed.returncode:
        lines = completed.stderr.strip().splitlines()
        raise RuntimeError(f"{description}: {lines[-1] if lines else 'failed'}")
    return completed


def inspect(path: Path) -> Media:
    completed = run(
        [
            "ffprobe", "-v", "error", "-show_streams", "-show_format",
            "-of", "json", str(path),
        ],
        f"inspect {path}",
    )
    payload = json.loads(completed.stdout)
    streams = payload.get("streams", [])
    videos = [item for item in streams if item.get("codec_type") == "video"]
    audios = [item for item in streams if item.get("codec_type") == "audio"]
    subtitles = [item for item in streams if item.get("codec_type") == "subtitle"]
    if len(videos) != 1:
        raise ValueError(f"expected one video stream in {path}")
    video = videos[0]
    metadata = payload.get("format", {})
    duration = float(metadata.get("duration") or video.get("duration") or 0)
    size = path.stat().st_size
    bitrate = int(metadata.get("bit_rate") or (size * 8 / duration if duration else 0))
    return Media(
        path=path, size=size, duration=duration,
        width=int(video.get("width") or 0), height=int(video.get("height") or 0),
        bitrate=bitrate, audio_streams=len(audios), subtitle_streams=len(subtitles),
    )


def candidates(root: Path, scan_jobs: int) -> tuple[list[Media], list[dict[str, str]]]:
    files = sorted({
        path.resolve() for path in root.rglob("*")
        if path.is_file()
        and not path.is_symlink()
        and "_Review" not in path.parts
        and path.suffix.lower() in MEDIA_SUFFIXES
    })
    media: list[Media] = []
    failures: list[dict[str, str]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=scan_jobs) as executor:
        futures = {executor.submit(inspect, path): path for path in files}
        for future in concurrent.futures.as_completed(futures):
            path = futures[future]
            try:
                item = future.result()
                # Restrict destructive replacement to layouts this pipeline preserves exactly.
                if (
                    item.path.suffix.lower() == ".mp4"
                    and item.audio_streams <= 1
                    and item.subtitle_streams == 0
                    and item.height <= 576
                    and item.bitrate >= 1_500_000
                    and item.size >= 100 * 1024 * 1024
                ):
                    media.append(item)
            except (OSError, RuntimeError, ValueError) as error:
                failures.append({"source": str(path), "error": str(error)})
    return sorted(media, key=lambda item: item.size, reverse=True), failures


def append_journal(path: Path, payload: dict[str, Any]) -> None:
    with JOURNAL_LOCK:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())


def completed_sources(path: Path) -> set[str]:
    if not path.exists():
        return set()
    completed: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if item.get("status") in {"replaced", "kept-source-smaller"}:
            completed.add(item.get("source", ""))
    return completed


def optimize(
    item: Media,
    *,
    quality: int,
    audio_bitrate: int,
    minimum_savings: float,
    lightning: bool,
    video_bitrate: int,
) -> Result:
    source = item.path
    video_part = source.with_name(f".{source.name}.video-part.mp4")
    final_part = source.with_name(f".{source.name}.verified-part.mp4")
    video_part.unlink(missing_ok=True)
    final_part.unlink(missing_ok=True)
    try:
        handbrake = [
            "HandBrakeCLI", "-i", str(source), "-o", str(video_part),
            "-Z", "Fast 480p30", "-E", "copy", "--vfr", "-O",
        ]
        if lightning:
            handbrake.extend(
                [
                    "-e", "vt_h264", "--encoder-preset", "speed",
                    "-b", str(video_bitrate), "--no-multi-pass",
                    "--enable-hw-decoding", "videotoolbox",
                ]
            )
        else:
            handbrake.extend(["-e", "x264", "-q", str(quality)])
        run(
            handbrake,
            f"encode {source}",
        )
        run(
            [
                "ffmpeg", "-nostdin", "-hide_banner", "-v", "error",
                "-i", str(video_part), "-map", "0:v:0", "-map", "0:a?",
                "-c:v", "copy", "-c:a", "aac", "-b:a", f"{audio_bitrate}k",
                "-map_metadata", "0", "-movflags", "+faststart", "-y", str(final_part),
            ],
            f"finish audio {source}",
        )
        output = inspect(final_part)
        if output.audio_streams != item.audio_streams or output.subtitle_streams != 0:
            raise RuntimeError(f"stream layout changed for {source}")
        if abs(output.duration - item.duration) > max(1.0, item.duration * 0.01):
            raise RuntimeError(f"duration changed for {source}")
        run(
            [
                "ffmpeg", "-nostdin", "-hide_banner", "-v", "error",
                "-i", str(final_part), "-f", "null", "-",
            ],
            f"decode verification {source}",
        )
        savings = item.size - output.size
        if savings < item.size * minimum_savings:
            return Result(str(source), "kept-source-smaller", item.size, output.size, 0, item.duration)
        os.replace(final_part, source)
        return Result(str(source), "replaced", item.size, output.size, savings, output.duration)
    finally:
        video_part.unlink(missing_ok=True)
        final_part.unlink(missing_ok=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--quality", type=int, default=22)
    parser.add_argument("--audio-bitrate", type=int, default=96)
    parser.add_argument("--minimum-savings", type=float, default=0.20)
    parser.add_argument("--jobs", type=int, default=6)
    parser.add_argument("--scan-jobs", type=int, default=16)
    parser.add_argument(
        "--lightning", action="store_true",
        help="use Apple VideoToolbox H.264 hardware encoding",
    )
    parser.add_argument(
        "--video-bitrate", type=int, default=1200,
        help="Lightning mode video bitrate in kbit/s (default: 1200)",
    )
    parser.add_argument("--journal", type=Path, default=Path(".media-optimize-journal.jsonl"))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.root.is_dir() or not 0 < args.minimum_savings < 1 or args.jobs < 1:
        print("error: invalid root, savings threshold, or job count", file=sys.stderr)
        return 2
    for executable in ("HandBrakeCLI", "ffmpeg", "ffprobe"):
        if shutil.which(executable) is None:
            print(f"error: {executable} is required", file=sys.stderr)
            return 2

    print("Scanning media metadata...", flush=True)
    items, scan_failures = candidates(args.root, args.scan_jobs)
    done = completed_sources(args.journal)
    items = [item for item in items if str(item.path) not in done]
    print(f"Selected {len(items)} worthwhile candidates; {len(scan_failures)} scan failures.", flush=True)
    for failure in scan_failures:
        append_journal(args.journal, {"status": "scan-failed", **failure})

    total_saved = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as executor:
        futures = {
            executor.submit(
                optimize, item, quality=args.quality,
                audio_bitrate=args.audio_bitrate, minimum_savings=args.minimum_savings,
                lightning=args.lightning, video_bitrate=args.video_bitrate,
            ): item
            for item in items
        }
        for index, future in enumerate(concurrent.futures.as_completed(futures), 1):
            item = futures[future]
            try:
                result = future.result()
                total_saved += result.savings_bytes
                append_journal(args.journal, asdict(result))
                print(
                    f"[{index}/{len(items)}] {result.status} {item.path.name}; "
                    f"saved {result.savings_bytes / 2**30:.2f} GiB; "
                    f"run total {total_saved / 2**30:.2f} GiB",
                    flush=True,
                )
            except (OSError, RuntimeError, ValueError) as error:
                append_journal(args.journal, {"source": str(item.path), "status": "failed", "error": str(error)})
                print(f"[{index}/{len(items)}] FAILED {item.path}: {error}", file=sys.stderr, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
