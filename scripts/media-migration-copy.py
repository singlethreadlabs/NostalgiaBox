#!/usr/bin/env python3
"""Copy a canonical media manifest sequentially with hash verification and resume."""

from __future__ import annotations

import argparse
import csv
import errno
import hashlib
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO


CHUNK_SIZE = 8 * 1024 * 1024


def append_journal(path: Path, record: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, sort_keys=True) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def completed_destinations(path: Path) -> dict[str, dict[str, object]]:
    if not path.exists():
        return {}
    completed: dict[str, dict[str, object]] = {}
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        try:
            record = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError(f"invalid journal JSON on line {line_number}") from error
        if record.get("status") == "verified":
            completed[str(record["destination"])] = record
    return completed


def hash_stream(source: BinaryIO, destination: BinaryIO | None = None) -> str:
    digest = hashlib.sha256()
    while chunk := source.read(CHUNK_SIZE):
        digest.update(chunk)
        if destination is not None:
            destination.write(chunk)
    return digest.hexdigest()


def hash_file(path: Path) -> str:
    with path.open("rb") as stream:
        return hash_stream(stream)


def sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        try:
            os.fsync(descriptor)
        except OSError as error:
            if error.errno != errno.EINVAL:
                raise
    finally:
        os.close(descriptor)


def copy_one(source: Path, destination: Path, expected_size: int) -> dict[str, object]:
    stat = source.stat()
    if stat.st_size != expected_size:
        raise RuntimeError(
            f"source size changed: expected {expected_size}, got {stat.st_size}: {source}"
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(f".{destination.name}.migration-part")
    partial.unlink(missing_ok=True)
    try:
        with source.open("rb") as input_stream, partial.open("xb") as output_stream:
            source_hash = hash_stream(input_stream, output_stream)
            output_stream.flush()
            os.fsync(output_stream.fileno())
        if partial.stat().st_size != expected_size:
            raise RuntimeError(f"copied size mismatch: {destination}")
        os.replace(partial, destination)
        sync_directory(destination.parent)
        destination_hash = hash_file(destination)
        if destination_hash != source_hash:
            destination.unlink(missing_ok=True)
            raise RuntimeError(f"SHA256 mismatch: {destination}")
        return {
            "status": "verified",
            "source": str(source),
            "destination": str(destination),
            "size": expected_size,
            "sha256": source_hash,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    finally:
        partial.unlink(missing_ok=True)


def load_manifest(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as stream:
        rows = list(csv.DictReader(stream))
    required = {"source", "destination", "size", "confidence", "reason"}
    if not rows or not required.issubset(rows[0]):
        raise ValueError("manifest is empty or missing required columns")
    if len({row["destination"].casefold() for row in rows}) != len(rows):
        raise ValueError("manifest contains destination collisions")
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--destination-root", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--journal", required=True, type=Path)
    args = parser.parse_args()

    source_root = args.source_root.resolve()
    destination_root = args.destination_root.resolve()
    rows = load_manifest(args.manifest.resolve())
    required_bytes = sum(int(row["size"]) for row in rows)
    destination_root.mkdir(parents=True, exist_ok=True)
    available_bytes = shutil.disk_usage(destination_root).free
    completed = completed_destinations(args.journal.resolve())
    remaining_bytes = sum(
        int(row["size"])
        for row in rows
        if str(destination_root / row["destination"]) not in completed
    )
    if available_bytes < remaining_bytes:
        print(
            f"error: destination has {available_bytes} bytes free but "
            f"{remaining_bytes} bytes remain",
            flush=True,
        )
        return 2
    print(
        f"files={len(rows)} total_bytes={required_bytes} "
        f"completed={len(completed)} remaining_bytes={remaining_bytes}",
        flush=True,
    )

    failures = 0
    for index, row in enumerate(rows, 1):
        source = source_root / row["source"]
        destination = destination_root / row["destination"]
        existing = completed.get(str(destination))
        if existing:
            if not destination.is_file() or destination.stat().st_size != int(row["size"]):
                print(f"error: completed destination missing or changed: {destination}", flush=True)
                return 2
            continue
        try:
            record = copy_one(source, destination, int(row["size"]))
        except (OSError, RuntimeError, ValueError) as error:
            failures += 1
            record = {
                "status": "failed",
                "source": str(source),
                "destination": str(destination),
                "reason": str(error),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        append_journal(args.journal.resolve(), record)
        print(f"[{index}/{len(rows)}] {record['status']}: {destination}", flush=True)
        if record["status"] == "failed":
            break

    if failures:
        print(f"Stopped after {failures} failure.", flush=True)
        return 1
    print(f"Finished {len(rows)} files with 0 failures.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
