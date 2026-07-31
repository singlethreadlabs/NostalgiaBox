#!/usr/bin/env python3
"""Create verified, normalized media outputs without touching source files."""

from __future__ import annotations

import argparse
import concurrent.futures
import importlib.util
import json
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


OPTIMIZER_PATH = Path(__file__).with_name("media-optimize.py")
SPEC = importlib.util.spec_from_file_location("media_optimize", OPTIMIZER_PATH)
optimizer = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = optimizer
SPEC.loader.exec_module(optimizer)

# Leave AAC headroom while dynamic loudness mode controls difficult peaks.
ENCODE_TRUE_PEAK_DBTP = -2.5


@dataclass(frozen=True)
class StageResult:
    source: str
    output: str
    video_action: str
    audio_action: str
    source_bytes: int
    output_bytes: int
    savings_bytes: int
    source_lufs: float | None
    output_lufs: float | None
    source_true_peak_dbtp: float | None
    output_true_peak_dbtp: float | None
    duration: float
    verified: bool


@dataclass(frozen=True)
class StreamLayout:
    audio_codecs: tuple[str, ...]
    subtitle_codecs: tuple[str, ...]


TEXT_SUBTITLE_CODECS = {"ass", "mov_text", "ssa", "subrip", "webvtt"}


def run(command: list[str], *, description: str) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        detail = result.stderr.strip().splitlines()
        raise RuntimeError(f"{description}: {detail[-1] if detail else 'command failed'}")
    return result


def probe_stream_layout(path: Path) -> StreamLayout:
    result = run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "stream=codec_type,codec_name",
            "-of",
            "json",
            str(path),
        ],
        description=f"inspect streams for {path}",
    )
    streams = json.loads(result.stdout).get("streams", [])
    return StreamLayout(
        audio_codecs=tuple(
            stream.get("codec_name", "")
            for stream in streams
            if stream.get("codec_type") == "audio"
        ),
        subtitle_codecs=tuple(
            stream.get("codec_name", "")
            for stream in streams
            if stream.get("codec_type") == "subtitle"
        ),
    )


def measure_segment(
    path: Path, *, start: float, duration: float | None
) -> optimizer.LoudnessMeasurement:
    command = [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-v",
        "info",
    ]
    if start:
        command.extend(["-ss", f"{start:.3f}"])
    command.extend(["-i", str(path)])
    if duration is not None:
        command.extend(["-t", f"{duration:.3f}"])
    command.extend(
        [
            "-map",
            "0:a:0",
            "-vn",
            "-sn",
            "-dn",
            "-af",
            (
                f"loudnorm=I={optimizer.TARGET_INTEGRATED_LUFS}:"
                f"TP={ENCODE_TRUE_PEAK_DBTP}:"
                f"LRA={optimizer.TARGET_LOUDNESS_RANGE_LU}:print_format=json"
            ),
            "-f",
            "null",
            "-",
        ]
    )
    result = run(command, description=f"measure loudness for {path}")
    return optimizer.parse_loudness_output(path, result.stderr)


def loudnorm_filter(measurement: optimizer.LoudnessMeasurement) -> str:
    return (
        f"loudnorm=I={optimizer.TARGET_INTEGRATED_LUFS}:"
        f"TP={ENCODE_TRUE_PEAK_DBTP}:"
        f"LRA={optimizer.TARGET_LOUDNESS_RANGE_LU}:"
        f"measured_I={measurement.integrated_lufs}:"
        f"measured_TP={measurement.true_peak_dbtp}:"
        f"measured_LRA={measurement.loudness_range_lu}:"
        f"measured_thresh={measurement.threshold}:"
        f"offset={measurement.offset}:linear=false:print_format=summary"
    )


def output_name(source: Path, *, sample_seconds: float | None) -> str:
    suffix = f".sample-{sample_seconds:g}s" if sample_seconds is not None else ""
    return f"{source.stem}{suffix}.mp4"


def relative_output(
    source: Path, roots: list[Path], *, sample_seconds: float | None
) -> Path:
    for root in roots:
        resolved = root.resolve()
        if root.is_dir() and source.is_relative_to(resolved):
            relative = source.relative_to(resolved)
            return relative.with_name(
                output_name(relative, sample_seconds=sample_seconds)
            )
    return Path(output_name(source, sample_seconds=sample_seconds))


def should_encode_video(info: optimizer.MediaInfo, *, max_height: int) -> bool:
    recommendation = optimizer.recommend(info, max_height=max_height)
    return (
        info.video_codec != "h264"
        or info.height > max_height
        or "estimated size reduction is meaningful" in recommendation.reasons
    )


def repair_normalized_audio(
    path: Path, measurement: optimizer.LoudnessMeasurement, audio_bitrate: str
) -> None:
    repaired = path.with_suffix(".repair.mp4")
    command = [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-v",
        "warning",
        "-i",
        str(path),
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        "-map",
        "0:s?",
        "-c:v",
        "copy",
        "-c:a",
        "aac",
        "-b:a",
        audio_bitrate,
        "-filter:a:0",
        loudnorm_filter(measurement),
        "-c:s",
        "copy",
        "-map_metadata",
        "0",
        "-movflags",
        "+faststart",
        "-y",
        str(repaired),
    ]
    try:
        run(command, description=f"repair codec-induced audio peaks for {path}")
        repaired.replace(path)
    finally:
        repaired.unlink(missing_ok=True)


def preserve_source_audio(path: Path, source: Path) -> None:
    preserved = path.with_suffix(".preserved-audio.mp4")
    command = [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-v",
        "warning",
        "-i",
        str(path),
        "-i",
        str(source),
        "-map",
        "0:v:0",
        "-map",
        "1:a?",
        "-map",
        "1:s?",
        "-c",
        "copy",
        "-map_metadata",
        "1",
        "-movflags",
        "+faststart",
        "-y",
        str(preserved),
    ]
    try:
        run(command, description=f"preserve source audio for {source}")
        preserved.replace(path)
    finally:
        preserved.unlink(missing_ok=True)


def stage_one(
    source: Path,
    output_dir: Path,
    *,
    max_height: int,
    crf: int,
    preset: str,
    audio_bitrate: str,
    start: float,
    sample_seconds: float | None,
    relative_destination: Path | None = None,
) -> StageResult:
    info = optimizer.probe_media(source)
    source_layout = probe_stream_layout(source)
    unsupported_subtitles = set(source_layout.subtitle_codecs) - TEXT_SUBTITLE_CODECS
    if unsupported_subtitles:
        raise RuntimeError(
            f"unsupported bitmap subtitle codec for MP4 output in {source}: "
            f"{', '.join(sorted(unsupported_subtitles))}"
        )
    destination = output_dir / (
        relative_destination
        or output_name(source, sample_seconds=sample_seconds)
    )
    if destination.exists():
        raise FileExistsError(f"staged output already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_suffix(".part.mp4")

    measurement = (
        measure_segment(source, start=start, duration=sample_seconds)
        if info.audio_codec is not None
        else None
    )
    normalize_audio = measurement is not None and (
        abs(measurement.integrated_lufs - optimizer.TARGET_INTEGRATED_LUFS)
        > optimizer.LOUDNESS_TOLERANCE_LU
        or measurement.true_peak_dbtp > optimizer.TARGET_TRUE_PEAK_DBTP
    )
    encode_video = should_encode_video(info, max_height=max_height)

    command = ["ffmpeg", "-nostdin", "-hide_banner", "-v", "warning"]
    if start:
        command.extend(["-ss", f"{start:.3f}"])
    command.extend(["-i", str(source)])
    if sample_seconds is not None:
        command.extend(["-t", f"{sample_seconds:.3f}"])
    command.extend(
        ["-map", "0:v:0", "-map", "0:a?", "-map", "0:s?", "-map_chapters", "0"]
    )
    if encode_video:
        command.extend(["-c:v", "libx264", "-preset", preset, "-crf", str(crf)])
        if info.height > max_height:
            command.extend(["-vf", f"scale=-2:{max_height}:flags=lanczos"])
    else:
        command.extend(["-c:v", "copy"])
    convert_audio = normalize_audio or any(
        codec != "aac" for codec in source_layout.audio_codecs
    )
    if convert_audio:
        assert measurement is not None
        command.extend(["-c:a", "aac", "-b:a", audio_bitrate])
        if normalize_audio:
            command.extend(["-filter:a:0", loudnorm_filter(measurement)])
    elif source_layout.audio_codecs:
        command.extend(["-c:a", "copy"])
    if source_layout.subtitle_codecs:
        command.extend(["-c:s", "mov_text"])
    command.extend(
        [
            "-map_metadata",
            "0",
            "-movflags",
            "+faststart",
            "-y",
            str(partial),
        ]
    )

    try:
        run(command, description=f"stage {source}")
        staged_info = optimizer.probe_media(partial)
        staged_layout = probe_stream_layout(partial)
        if len(staged_layout.audio_codecs) != len(source_layout.audio_codecs):
            raise RuntimeError(f"audio stream count changed for {source}")
        if len(staged_layout.subtitle_codecs) != len(source_layout.subtitle_codecs):
            raise RuntimeError(f"subtitle stream count changed for {source}")
        expected_duration = sample_seconds or max(0.0, info.duration - start)
        if abs(staged_info.duration - expected_duration) > max(1.0, expected_duration * 0.01):
            raise RuntimeError(
                f"duration mismatch for {source}: expected {expected_duration:.3f}s, "
                f"got {staged_info.duration:.3f}s"
            )
        run(
            [
                "ffmpeg",
                "-nostdin",
                "-hide_banner",
                "-v",
                "error",
                "-i",
                str(partial),
                "-f",
                "null",
                "-",
            ],
            description=f"decode verification for {source}",
        )
        output_measurement = (
            optimizer.measure_loudness(partial)
            if staged_info.audio_codec is not None
            else None
        )
        if normalize_audio and output_measurement is not None:
            repaired_audio = False
            for _ in range(1):
                outside_target = (
                    abs(
                        output_measurement.integrated_lufs
                        - optimizer.TARGET_INTEGRATED_LUFS
                    )
                    > 0.75
                    or output_measurement.true_peak_dbtp
                    > optimizer.TARGET_TRUE_PEAK_DBTP
                )
                if not outside_target:
                    break
                repair_normalized_audio(partial, output_measurement, audio_bitrate)
                repaired_audio = True
                output_measurement = optimizer.measure_loudness(partial)
            if repaired_audio:
                run(
                    [
                        "ffmpeg",
                        "-nostdin",
                        "-hide_banner",
                        "-v",
                        "error",
                        "-i",
                        str(partial),
                        "-f",
                        "null",
                        "-",
                    ],
                    description=f"repaired decode verification for {source}",
                )
            outside_target = (
                abs(
                    output_measurement.integrated_lufs
                    - optimizer.TARGET_INTEGRATED_LUFS
                )
                > 0.75
                or output_measurement.true_peak_dbtp
                > optimizer.TARGET_TRUE_PEAK_DBTP
            )
            if outside_target:
                preserve_source_audio(partial, source)
                run(
                    [
                        "ffmpeg",
                        "-nostdin",
                        "-hide_banner",
                        "-v",
                        "error",
                        "-i",
                        str(partial),
                        "-f",
                        "null",
                        "-",
                    ],
                    description=f"preserved-audio decode verification for {source}",
                )
                output_measurement = optimizer.measure_loudness(partial)
                normalize_audio = False
                audio_fallback = True
            else:
                audio_fallback = False
        else:
            audio_fallback = False
        partial.replace(destination)
    except Exception:
        partial.unlink(missing_ok=True)
        raise

    output_bytes = destination.stat().st_size
    audio_action = "none"
    if normalize_audio:
        audio_action = "normalize"
    elif audio_fallback:
        audio_action = "preserve-peak-outlier"
    elif info.audio_codec == "aac":
        audio_action = "copy"
    elif source_layout.audio_codecs:
        audio_action = "convert"

    return StageResult(
        source=str(source),
        output=str(destination),
        video_action="encode" if encode_video else "copy",
        audio_action=audio_action,
        source_bytes=info.size,
        output_bytes=output_bytes,
        savings_bytes=max(0, info.size - output_bytes) if sample_seconds is None else 0,
        source_lufs=measurement.integrated_lufs if measurement else None,
        output_lufs=output_measurement.integrated_lufs if output_measurement else None,
        source_true_peak_dbtp=measurement.true_peak_dbtp if measurement else None,
        output_true_peak_dbtp=(
            output_measurement.true_peak_dbtp if output_measurement else None
        ),
        duration=staged_info.duration,
        verified=True,
    )


def write_manifest(
    output_dir: Path, results: list[StageResult], failures: list[dict[str, str]]
) -> None:
    payload: dict[str, Any] = {
        "status": "staged-for-review",
        "replace_sources": False,
        "files": [asdict(result) for result in results],
        "failures": failures,
    }
    manifest = output_dir / "manifest.json"
    manifest.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create verified optimized media in a staging directory."
    )
    parser.add_argument("paths", nargs="+", type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--max-height", type=int, default=480)
    parser.add_argument("--crf", type=int, default=24)
    parser.add_argument("--preset", default="medium")
    parser.add_argument("--audio-bitrate", default="128k")
    parser.add_argument(
        "--target-lufs",
        type=float,
        default=-16.0,
        help="integrated loudness target (episodes: -20; bumpers: -16)",
    )
    parser.add_argument("--start", type=float, default=0.0)
    parser.add_argument("--sample-seconds", type=float)
    parser.add_argument(
        "--jobs", type=int, default=2, help="simultaneous staging jobs (default: 2)"
    )
    parser.add_argument(
        "--resume", action="store_true", help="resume an interrupted staging manifest"
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        print("error: ffmpeg and ffprobe are required", file=sys.stderr)
        return 2
    if (
        not 144 <= args.max_height
        or not 0 <= args.crf <= 51
        or args.jobs < 1
        or not -30 <= args.target_lufs <= -10
    ):
        print(
            "error: --max-height must be >= 144, --crf must be 0-51, "
            "--jobs must be >= 1, and --target-lufs must be -30 to -10",
            file=sys.stderr,
        )
        return 2
    optimizer.TARGET_INTEGRATED_LUFS = args.target_lufs
    if args.start < 0 or (
        args.sample_seconds is not None and args.sample_seconds <= 0
    ):
        print("error: --start must be >= 0 and --sample-seconds must be > 0", file=sys.stderr)
        return 2

    files = optimizer.find_media(args.paths)
    if not files:
        print("error: no media files found", file=sys.stderr)
        return 1
    args.output_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = args.output_dir / "manifest.json"
    if any(args.output_dir.iterdir()) and not args.resume:
        print("error: --output-dir must be empty", file=sys.stderr)
        return 1

    results: list[StageResult] = []
    if args.resume and manifest_path.exists():
        manifest_data = json.loads(manifest_path.read_text(encoding="utf-8"))
        results = [StageResult(**item) for item in manifest_data.get("files", [])]
        for result in results:
            if not Path(result.output).is_file():
                print(f"error: completed staged output is missing: {result.output}", file=sys.stderr)
                return 1

    destinations = {
        source: relative_output(
            source, args.paths, sample_seconds=args.sample_seconds
        )
        for source in files
    }
    if len(set(destinations.values())) != len(destinations):
        print(
            "error: input paths produce duplicate staged filenames; "
            "process the roots separately",
            file=sys.stderr,
        )
        return 1

    completed = {result.source for result in results}
    failures: list[dict[str, str]] = []
    pending = [source for source in files if str(source) not in completed]
    if args.resume:
        for source in pending:
            destination = args.output_dir / destinations[source]
            destination.unlink(missing_ok=True)
            destination.with_suffix(".part.mp4").unlink(missing_ok=True)
    for source in files:
        if str(source) in completed:
            print(f"already verified {source}")

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as executor:
        future_sources = {
            executor.submit(
                stage_one,
                source,
                args.output_dir,
                max_height=args.max_height,
                crf=args.crf,
                preset=args.preset,
                audio_bitrate=args.audio_bitrate,
                start=args.start,
                sample_seconds=args.sample_seconds,
                relative_destination=destinations[source],
            ): source
            for source in pending
        }
        for completed_count, future in enumerate(
            concurrent.futures.as_completed(future_sources),
            start=len(completed) + 1,
        ):
            source = future_sources[future]
            try:
                result = future.result()
                results.append(result)
                print(
                    f"[{completed_count}/{len(files)}] verified {source.name}: "
                    f"{result.output_lufs:.1f} LUFS, "
                    f"{result.output_true_peak_dbtp:.1f} dBTP"
                    if result.output_lufs is not None
                    else f"[{completed_count}/{len(files)}] verified {source.name}"
                )
            except (OSError, RuntimeError, ValueError) as error:
                failures.append({"source": str(source), "error": str(error)})
                print(f"error: {error}", file=sys.stderr)
            write_manifest(args.output_dir, results, failures)

    write_manifest(args.output_dir, results, failures)
    print(f"Staged and verified {len(results)} files in {args.output_dir}.")
    print("Source files were not modified. Review manifest.json before replacement.")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
