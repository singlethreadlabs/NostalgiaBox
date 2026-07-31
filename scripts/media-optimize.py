#!/usr/bin/env python3
"""Analyze a media library and estimate safe normalization opportunities."""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import re
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Iterable


VIDEO_EXTENSIONS = {
    ".avi", ".m4v", ".mkv", ".mov", ".mp4", ".mpeg", ".mpg", ".ts", ".webm",
}
DIRECT_CONTAINERS = {"mov", "mp4", "m4a", "3gp", "3g2", "mj2"}
TARGET_AUDIO_BITRATE = 96_000
TARGET_INTEGRATED_LUFS = -16.0
TARGET_TRUE_PEAK_DBTP = -1.5
TARGET_LOUDNESS_RANGE_LU = 11.0
LOUDNESS_TOLERANCE_LU = 1.0


@dataclass(frozen=True)
class MediaInfo:
    path: Path
    size: int
    duration: float
    width: int
    height: int
    video_codec: str | None
    audio_codec: str | None
    audio_bitrate: int
    format_names: tuple[str, ...]

    @property
    def direct_play(self) -> bool:
        return (
            self.video_codec == "h264"
            and self.audio_codec in {"aac", None}
            and bool(set(self.format_names) & DIRECT_CONTAINERS)
        )


@dataclass(frozen=True)
class Recommendation:
    path: str
    action: str
    reasons: tuple[str, ...]
    current_bytes: int
    estimated_bytes: int
    estimated_savings_bytes: int
    width: int
    height: int
    video_codec: str | None
    audio_codec: str | None
    direct_play: bool
    integrated_lufs: float | None = None
    true_peak_dbtp: float | None = None
    loudness_range_lu: float | None = None
    normalize_audio: bool | None = None


@dataclass(frozen=True)
class LoudnessMeasurement:
    integrated_lufs: float
    true_peak_dbtp: float
    loudness_range_lu: float
    threshold: float = 0.0
    offset: float = 0.0


def positive_int(value: Any) -> int:
    try:
        parsed = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return max(parsed, 0)


def probe_media(path: Path, *, timeout: float = 30.0) -> MediaInfo:
    result = subprocess.run(
        [
            "ffprobe", "-v", "error", "-print_format", "json",
            "-show_format", "-show_streams", str(path),
        ],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or "unknown ffprobe error"
        raise ValueError(f"{path}: {detail}")

    data = json.loads(result.stdout or "{}")
    streams = data.get("streams") or []
    video = next((item for item in streams if item.get("codec_type") == "video"), {})
    audio = next((item for item in streams if item.get("codec_type") == "audio"), {})
    format_data = data.get("format") or {}
    duration = float(format_data.get("duration") or video.get("duration") or 0)
    size = positive_int(format_data.get("size")) or path.stat().st_size
    if duration <= 0 or size <= 0 or not video:
        raise ValueError(f"{path}: missing positive duration, size, or video stream")

    return MediaInfo(
        path=path,
        size=size,
        duration=duration,
        width=positive_int(video.get("width")),
        height=positive_int(video.get("height")),
        video_codec=video.get("codec_name"),
        audio_codec=audio.get("codec_name"),
        audio_bitrate=positive_int(audio.get("bit_rate")),
        format_names=tuple(
            name.strip()
            for name in str(format_data.get("format_name") or "").split(",")
            if name.strip()
        ),
    )


def measure_loudness(path: Path, *, timeout: float | None = None) -> LoudnessMeasurement:
    """Measure the first audio stream using FFmpeg's EBU R128 loudnorm filter."""
    result = subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-v",
            "info",
            "-i",
            str(path),
            "-map",
            "0:a:0",
            "-vn",
            "-sn",
            "-dn",
            "-af",
            (
                f"loudnorm=I={TARGET_INTEGRATED_LUFS}:TP={TARGET_TRUE_PEAK_DBTP}:"
                f"LRA={TARGET_LOUDNESS_RANGE_LU}:print_format=json"
            ),
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode != 0:
        detail = (
            result.stderr.strip().splitlines()[-1]
            if result.stderr.strip()
            else "unknown FFmpeg error"
        )
        raise ValueError(f"{path}: {detail}")

    return parse_loudness_output(path, result.stderr)


def parse_loudness_output(path: Path, output: str) -> LoudnessMeasurement:
    """Parse the JSON summary emitted by FFmpeg's loudnorm filter."""
    matches = re.findall(r"\{[^{}]*\}", output, flags=re.DOTALL)

    if not matches:
        raise ValueError(f"{path}: FFmpeg returned no loudness measurement")
    try:
        data = json.loads(matches[-1])
        return LoudnessMeasurement(
            integrated_lufs=float(data["input_i"]),
            true_peak_dbtp=float(data["input_tp"]),
            loudness_range_lu=float(data["input_lra"]),
            threshold=float(data.get("input_thresh", 0.0)),
            offset=float(data.get("target_offset", 0.0)),
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise ValueError(f"{path}: invalid FFmpeg loudness measurement") from error


def target_video_bitrate(height: int) -> int:
    """Return a conservative planning bitrate for H.264 CRF 24 output."""
    if height <= 360:
        return 700_000
    if height <= 480:
        return 1_000_000
    return 1_600_000


def recommend(info: MediaInfo, *, max_height: int) -> Recommendation:
    output_height = min(info.height or max_height, max_height)
    estimated_bitrate = target_video_bitrate(output_height)
    if info.audio_codec is not None:
        estimated_bitrate += TARGET_AUDIO_BITRATE
    estimated_bytes = round(info.duration * estimated_bitrate / 8)

    reasons: list[str] = []
    if info.height > max_height:
        reasons.append(f"resolution exceeds {max_height}p")
    if not info.direct_play:
        reasons.append("requires remux or transcode for browser delivery")

    possible_savings = info.size - estimated_bytes
    if possible_savings >= max(round(info.size * 0.15), 50 * 1024 * 1024):
        reasons.append("estimated size reduction is meaningful")
        if info.audio_bitrate > 128_000:
            reasons.append("audio bitrate exceeds 128 kbps")

    action = "optimize" if reasons else "keep"
    if action == "keep":
        estimated_bytes = info.size
        possible_savings = 0
    else:
        # Compatibility normalization may still be useful when no size is saved.
        estimated_bytes = min(estimated_bytes, info.size)
        possible_savings = info.size - estimated_bytes

    return Recommendation(
        path=str(info.path),
        action=action,
        reasons=tuple(reasons),
        current_bytes=info.size,
        estimated_bytes=estimated_bytes,
        estimated_savings_bytes=possible_savings,
        width=info.width,
        height=info.height,
        video_codec=info.video_codec,
        audio_codec=info.audio_codec,
        direct_play=info.direct_play,
    )


def add_loudness(
    recommendation: Recommendation, measurement: LoudnessMeasurement
) -> Recommendation:
    normalize_audio = (
        abs(measurement.integrated_lufs - TARGET_INTEGRATED_LUFS)
        > LOUDNESS_TOLERANCE_LU
        or measurement.true_peak_dbtp > TARGET_TRUE_PEAK_DBTP
    )
    return replace(
        recommendation,
        integrated_lufs=measurement.integrated_lufs,
        true_peak_dbtp=measurement.true_peak_dbtp,
        loudness_range_lu=measurement.loudness_range_lu,
        normalize_audio=normalize_audio,
    )


def find_media(paths: Iterable[Path]) -> list[Path]:
    files: set[Path] = set()
    for path in paths:
        if path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS:
            files.add(path.resolve())
        elif path.is_dir():
            files.update(
                candidate.resolve()
                for candidate in path.rglob("*")
                if candidate.is_file() and candidate.suffix.lower() in VIDEO_EXTENSIONS
            )
    return sorted(files, key=lambda item: str(item).lower())


def human_size(value: int) -> str:
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.1f} {unit}"
        size /= 1024
    raise AssertionError("unreachable")


def print_text(
    recommendations: list[Recommendation], failures: list[str], *, limit: int
) -> None:
    candidates = [item for item in recommendations if item.action == "optimize"]
    current = sum(item.current_bytes for item in recommendations)
    estimated = sum(item.estimated_bytes for item in recommendations)
    savings = current - estimated

    print(f"Analyzed {len(recommendations)} media files ({human_size(current)}).")
    print(
        f"Optimize {len(candidates)}; keep {len(recommendations) - len(candidates)}; "
        f"{len(failures)} could not be analyzed."
    )
    if current:
        print(
            f"Planning estimate: {human_size(savings)} recoverable "
            f"({savings / current * 100:.1f}%)."
        )
    else:
        print("Planning estimate: no media bytes analyzed.")
    print(
        "Estimate assumes H.264 CRF 24, AAC 96 kbps, and the selected height cap; "
        "actual output depends on source complexity."
    )

    ranked = sorted(
        candidates, key=lambda candidate: candidate.estimated_savings_bytes, reverse=True
    )
    shown = ranked if limit == 0 else ranked[:limit]
    for item in shown:
        print(
            f"{human_size(item.estimated_savings_bytes):>10}  "
            f"{item.height:>4}p  {item.path} ({'; '.join(item.reasons)})"
        )
    if len(shown) < len(ranked):
        print(
            f"... {len(ranked) - len(shown)} more candidates omitted; "
            "use --limit 0 or --json for all results."
        )

    measured = [item for item in recommendations if item.normalize_audio is not None]
    normalize = [item for item in measured if item.normalize_audio]
    if measured:
        print(
            f"Loudness audit: normalize {len(normalize)}; "
            f"within target {len(measured) - len(normalize)}."
        )
        print(
            f"Target: {TARGET_INTEGRATED_LUFS:.0f} LUFS, "
            f"{TARGET_TRUE_PEAK_DBTP:.1f} dBTP maximum, "
            f"{TARGET_LOUDNESS_RANGE_LU:.0f} LU loudness range."
        )
        ranked_audio = sorted(
            normalize,
            key=lambda candidate: abs(
                (
                    candidate.integrated_lufs
                    if candidate.integrated_lufs is not None
                    else TARGET_INTEGRATED_LUFS
                )
                - TARGET_INTEGRATED_LUFS
            ),
            reverse=True,
        )
        shown_audio = ranked_audio if limit == 0 else ranked_audio[:limit]
        for item in shown_audio:
            print(
                f"  {item.integrated_lufs:>6.1f} LUFS  "
                f"{item.true_peak_dbtp:>5.1f} dBTP  {item.path}"
            )
        if len(shown_audio) < len(ranked_audio):
            print(
                f"... {len(ranked_audio) - len(shown_audio)} "
                "more audio candidates omitted."
            )

    if failures:
        print("\nUnreadable files:", file=sys.stderr)
        for failure in failures:
            print(f"  {failure}", file=sys.stderr)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read-only media optimization analysis; no files are modified."
    )
    parser.add_argument(
        "paths",
        nargs="*",
        type=Path,
        default=[Path("downloads")],
        help="media files or directories to analyze (default: downloads)",
    )
    parser.add_argument(
        "--max-height",
        type=int,
        default=480,
        help="maximum planned output height (default: 480)",
    )
    parser.add_argument(
        "--jobs",
        type=int,
        default=4,
        help="simultaneous ffprobe processes (default: 4)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=25,
        help="maximum candidates to print; 0 prints all (default: 25)",
    )
    parser.add_argument("--json", action="store_true", help="emit machine-readable results")
    parser.add_argument(
        "--loudness",
        action="store_true",
        help="fully decode audio and audit EBU R128 loudness (slow; still read-only)",
    )
    parser.add_argument(
        "--loudness-jobs",
        type=int,
        default=1,
        help="simultaneous loudness measurements (default: 1)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if (
        args.max_height < 144
        or args.jobs < 1
        or args.loudness_jobs < 1
        or args.limit < 0
    ):
        print(
            "error: --max-height must be >= 144, --jobs and --loudness-jobs "
            "must be >= 1, and --limit must be >= 0",
            file=sys.stderr,
        )
        return 2
    if shutil.which("ffprobe") is None:
        print("error: ffprobe is required (install FFmpeg first)", file=sys.stderr)
        return 2
    if args.loudness and shutil.which("ffmpeg") is None:
        print("error: ffmpeg is required for --loudness", file=sys.stderr)
        return 2

    files = find_media(args.paths)
    if not files:
        print("error: no media files found", file=sys.stderr)
        return 1

    infos: list[MediaInfo] = []
    failures: list[str] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as executor:
        future_paths = {executor.submit(probe_media, path): path for path in files}
        for future in concurrent.futures.as_completed(future_paths):
            try:
                infos.append(future.result())
            except (OSError, ValueError, subprocess.TimeoutExpired, json.JSONDecodeError) as error:
                failures.append(str(error))

    recommendations = sorted(
        (recommend(info, max_height=args.max_height) for info in infos),
        key=lambda item: item.path.lower(),
    )
    if args.loudness:
        by_path = {item.path: item for item in recommendations}
        audible = [info for info in infos if info.audio_codec is not None]
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=args.loudness_jobs
        ) as executor:
            future_infos = {
                executor.submit(measure_loudness, info.path): info for info in audible
            }
            for future in concurrent.futures.as_completed(future_infos):
                info = future_infos[future]
                try:
                    by_path[str(info.path)] = add_loudness(
                        by_path[str(info.path)], future.result()
                    )
                except (OSError, ValueError, subprocess.TimeoutExpired) as error:
                    failures.append(str(error))
        recommendations = sorted(by_path.values(), key=lambda item: item.path.lower())
    if args.json:
        print(
            json.dumps(
                {
                    "assumptions": {
                        "video_codec": "h264",
                        "crf": 24,
                        "audio_codec": "aac",
                        "audio_bitrate": TARGET_AUDIO_BITRATE,
                        "max_height": args.max_height,
                        "integrated_lufs": TARGET_INTEGRATED_LUFS,
                        "true_peak_dbtp": TARGET_TRUE_PEAK_DBTP,
                        "loudness_range_lu": TARGET_LOUDNESS_RANGE_LU,
                    },
                    "files": [asdict(item) for item in recommendations],
                    "failures": failures,
                },
                indent=2,
            )
        )
    else:
        print_text(recommendations, failures, limit=args.limit)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
