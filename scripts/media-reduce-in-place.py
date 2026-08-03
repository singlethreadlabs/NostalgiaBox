#!/usr/bin/env python3
"""Reduce media one file at a time with verified atomic replacement."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import stat
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent


def load_script(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPT_DIR / filename)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {filename}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


stage = load_script("media_stage", "media-stage.py")
optimizer = stage.optimizer


def append_journal(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as output:
        output.write(json.dumps(record, sort_keys=True) + "\n")
        output.flush()
        os.fsync(output.fileno())


def completed_sources(path: Path, policy: dict[str, Any]) -> set[str]:
    if not path.exists():
        return set()
    completed: set[str] = set()
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        try:
            record = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError(f"invalid journal JSON on line {line_number}") from error
        if record.get("policy") == policy and record.get("status") in {
            "replaced",
            "retained",
        }:
            completed.add(str(record.get("source")))
    return completed


def sync_file(path: Path) -> None:
    with path.open("rb") as file:
        os.fsync(file.fileno())


def sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def candidate_path(source: Path) -> Path:
    return source.with_name(f".{source.stem}.reduce-candidate.mp4")


def policy_from_args(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "max_height": args.max_height,
        "crf": args.crf,
        "preset": args.preset,
        "audio_bitrate": args.audio_bitrate,
        "minimum_savings_percent": args.minimum_savings_percent,
        "minimum_savings_mib": args.minimum_savings_mib,
        "normalize_audio": not args.skip_loudness_normalization,
        "target_lufs": args.target_lufs,
    }


def reduce_one(
    source: Path,
    *,
    args: argparse.Namespace,
    policy: dict[str, Any],
) -> dict[str, Any]:
    if source.suffix.lower() != ".mp4":
        raise ValueError(f"in-place reduction requires an .mp4 source: {source}")
    before = source.stat()
    candidate = candidate_path(source)
    partial = candidate.with_suffix(".part.mp4")
    candidate.unlink(missing_ok=True)
    partial.unlink(missing_ok=True)
    timestamp = datetime.now(timezone.utc).isoformat()

    try:
        result = stage.stage_one(
            source,
            source.parent,
            max_height=args.max_height,
            crf=args.crf,
            preset=args.preset,
            audio_bitrate=args.audio_bitrate,
            start=0.0,
            sample_seconds=None,
            minimum_savings_percent=args.minimum_savings_percent,
            minimum_savings_bytes=round(args.minimum_savings_mib * 1024 * 1024),
            normalize_audio_enabled=not args.skip_loudness_normalization,
            relative_destination=Path(candidate.name),
        )
    except stage.SavingsGateError as error:
        return {
            "timestamp": timestamp,
            "status": "retained",
            "source": str(source),
            "source_bytes": before.st_size,
            "reason": str(error),
            "policy": policy,
        }

    after_encode = source.stat()
    if (
        after_encode.st_ino != before.st_ino
        or after_encode.st_size != before.st_size
        or after_encode.st_mtime_ns != before.st_mtime_ns
    ):
        candidate.unlink(missing_ok=True)
        raise RuntimeError(f"source changed while encoding; retained original: {source}")

    os.chmod(candidate, stat.S_IMODE(before.st_mode))
    sync_file(candidate)
    os.replace(candidate, source)
    sync_directory(source.parent)
    record = asdict(result)
    record.update(
        {
            "timestamp": timestamp,
            "status": "replaced",
            "source": str(source),
            "output": str(source),
            "policy": policy,
        }
    )
    return record


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Encode, verify, compare, and atomically replace media one file at a time."
        )
    )
    parser.add_argument("paths", nargs="*", type=Path)
    parser.add_argument("--paths-from", type=Path)
    parser.add_argument("--journal", required=True, type=Path)
    parser.add_argument("--max-height", type=int, default=720)
    parser.add_argument("--crf", type=int, default=23)
    parser.add_argument("--preset", default="slow")
    parser.add_argument("--audio-bitrate", default="128k")
    parser.add_argument("--minimum-savings-percent", type=float, default=15.0)
    parser.add_argument("--minimum-savings-mib", type=float, default=50.0)
    parser.add_argument("--target-lufs", type=float, default=-20.0)
    parser.add_argument("--skip-loudness-normalization", action="store_true")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="perform verified replacements; without this flag only list inputs",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.paths_from:
        try:
            args.paths.extend(stage.read_paths_file(args.paths_from))
        except OSError as error:
            print(f"error: cannot read --paths-from: {error}", file=sys.stderr)
            return 2
    if not args.paths:
        print("error: provide media paths or --paths-from", file=sys.stderr)
        return 2
    if not 144 <= args.max_height or not 0 <= args.crf <= 51:
        print("error: invalid height or CRF", file=sys.stderr)
        return 2
    if not 0 <= args.minimum_savings_percent <= 100 or args.minimum_savings_mib < 0:
        print("error: savings thresholds must be non-negative", file=sys.stderr)
        return 2
    try:
        stage.audio_bitrate_bps(args.audio_bitrate)
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    files = [
        path
        for path in optimizer.find_media(args.paths)
        if ".reduce-candidate" not in path.name
    ]
    if not files:
        print("error: no media files found", file=sys.stderr)
        return 1
    policy = policy_from_args(args)
    try:
        completed = completed_sources(args.journal, policy)
    except (OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    pending = [source for source in files if str(source) not in completed]
    total_bytes = sum(source.stat().st_size for source in pending)
    print(
        f"Selected {len(files)} files; {len(pending)} pending "
        f"({optimizer.human_size(total_bytes)})."
    )
    if not args.apply:
        for source in pending:
            print(source)
        print("Dry run only. Pass --apply to process files sequentially.")
        return 0

    failures = 0
    for index, source in enumerate(pending, 1):
        try:
            record = reduce_one(source, args=args, policy=policy)
        except (OSError, RuntimeError, ValueError) as error:
            failures += 1
            record = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "status": "failed",
                "source": str(source),
                "reason": str(error),
                "policy": policy,
            }
        append_journal(args.journal, record)
        print(f"[{index}/{len(pending)}] {record['status']}: {source}", flush=True)

    print(f"Finished {len(pending)} files with {failures} failures.")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
