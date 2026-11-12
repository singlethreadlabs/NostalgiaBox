"""Media indexing and browser playback capability classification."""

from __future__ import annotations

import json
import logging
import math
import shutil
import subprocess
import time
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

from nostalgiabox.channel import scan_episodes
from nostalgiabox.config import Config

from .database import Database


log = logging.getLogger(__name__)


@dataclass(frozen=True)
class MediaProbe:
    duration: float
    container: Optional[str]
    video_codec: Optional[str]
    audio_codec: Optional[str]

    @property
    def delivery_mode(self) -> str:
        containers = set((self.container or "").split(","))
        if (
            self.video_codec == "h264"
            and self.audio_codec in {"aac", None}
            and containers.intersection({"mov", "mp4", "m4a", "3gp", "3g2", "mj2"})
        ):
            return "direct"
        if self.video_codec == "h264" and self.audio_codec in {"aac", "mp3", None}:
            return "remux"
        return "transcode"


def probe_media(path: Path, *, timeout: float = 20.0) -> MediaProbe:
    if shutil.which("ffprobe") is None:
        raise RuntimeError("ffprobe is required to validate server media")
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode != 0:
        raise ValueError(f"ffprobe could not read media: {path}")
    data = json.loads(result.stdout or "{}")
    streams = data.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), {})
    audio = next((s for s in streams if s.get("codec_type") == "audio"), {})
    duration_raw = data.get("format", {}).get("duration") or video.get("duration")
    duration = float(duration_raw) if duration_raw else 0.0
    if not video.get("codec_name"):
        raise ValueError(f"media has no video stream: {path}")
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError(f"media has no positive duration: {path}")
    return MediaProbe(
        duration=duration,
        container=data.get("format", {}).get("format_name"),
        video_codec=video.get("codec_name"),
        audio_codec=audio.get("codec_name"),
    )


class MediaIndexer:
    def __init__(self, database: Database, config: Config, media_root: Path) -> None:
        self.database = database
        self.config = config
        self.media_root = media_root.resolve()
        self._observed: dict[str, tuple[int, int, float]] = {}
        self._rejected: dict[str, tuple[int, int]] = {}
        self.pending_count = 0

    @property
    def rejected_count(self) -> int:
        return len(self._rejected)

    def _validated_path(self, path: Path) -> Path:
        resolved = path.resolve()
        if not resolved.is_relative_to(self.media_root):
            raise ValueError(f"configured media path escapes {self.media_root}: {path}")
        if not resolved.exists():
            raise ValueError(f"configured media path is not accessible: {path}")
        return resolved

    def _files(self, roots: Iterable[Path]) -> list[Path]:
        files: set[Path] = set()
        for root in roots:
            validated = self._validated_path(root)
            if validated.is_file():
                if validated.suffix.lower() not in self.config.video_extensions:
                    raise ValueError(f"configured media file is not playable: {root}")
                files.add(validated)
                continue
            if not validated.is_dir():
                raise ValueError(f"configured media path is not a file or directory: {root}")
            files.update(
                scan_episodes(
                    validated,
                    self.config.video_extensions,
                    recursive=self.config.scan_recursive,
                )
            )
        return sorted(
            {self._validated_path(path) for path in files},
            key=lambda path: str(path).lower(),
        )

    def prepare(
        self, *, stable_seconds: float = 0, now: Optional[float] = None, bootstrap_channels=(),
    ):
        """Probe outside the writer transaction and wait for unchanged downloads."""
        now = time.time() if now is None else now
        assignments: list[tuple[int, str, str, Path]] = []
        for channel in self.config.channels:
            pools = {
                "show": channel.shows or (channel.path,),
                "bumper": channel.bumpers,
                "commercial": channel.commercials,
            }
            for kind, roots in pools.items():
                for root in roots:
                    pool_key = str(self._validated_path(root))
                    assignments.extend(
                        (channel.number, kind, pool_key, path)
                        for path in self._files((root,))
                    )
        with self.database.connect() as connection:
            existing = {
                row["path"]: row
                for row in connection.execute("SELECT * FROM media_items")
            }
        bootstrap_paths = {str(path) for number, _, _, path in assignments if number in bootstrap_channels}
        items: dict[str, dict] = {}
        seen: set[str] = set()
        for _, kind, _, path in assignments:
            resolved = str(path)
            if resolved in seen:
                continue
            seen.add(resolved)
            stat = path.stat()
            signature = (stat.st_size, stat.st_mtime_ns)
            if self._rejected.get(resolved) != signature:
                self._rejected.pop(resolved, None)
            previous = existing.get(resolved)
            if previous and signature == (previous["size"], previous["mtime_ns"]):
                items[resolved] = dict(previous)
                continue
            if stable_seconds and resolved not in bootstrap_paths:
                observed = self._observed.get(resolved)
                if observed is None or observed[:2] != signature:
                    self._observed[resolved] = (*signature, now)
                    continue
                if now - observed[2] < stable_seconds or self._rejected.get(resolved) == signature:
                    continue
            try:
                probe = probe_media(path)
                after = path.stat()
                if signature != (after.st_size, after.st_mtime_ns):
                    raise ValueError(f"media changed while probing: {path}")
            except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
                if not stable_seconds:
                    raise
                if isinstance(exc, ValueError):
                    self._rejected[resolved] = signature
                log.exception("media intake rejected %s", path)
                continue
            self._rejected.pop(resolved, None)
            items[resolved] = {
                "path": resolved, "kind": kind, "duration": probe.duration,
                "container": probe.container, "video_codec": probe.video_codec,
                "audio_codec": probe.audio_codec, "size": stat.st_size,
                "mtime_ns": stat.st_mtime_ns, "indexed_at": now,
            }
        paths = {str(path) for _, _, _, path in assignments}
        self._observed = {key: value for key, value in self._observed.items() if key in paths}
        self._rejected = {key: value for key, value in self._rejected.items() if key in paths}
        ready = [entry for entry in assignments if str(entry[3]) in items]
        self.pending_count = len(paths - items.keys() - self._rejected.keys())
        ready_channels = {number for number, kind, _, _ in ready if kind == "show"}
        missing = set(self.config.channel_numbers()) - ready_channels
        if missing:
            raise ValueError(f"no playable show media for channels {sorted(missing)}")
        return ready, items

    def refresh(self, *, scan=None, connection=None, rebuild_schedule: bool = False) -> int:
        assignments, items = self.prepare() if scan is None else scan
        now = time.time()
        transaction = self.database.connect() if connection is None else nullcontext(connection)
        with transaction as connection:
            for path, item in items.items():
                validated = self._validated_path(Path(path))
                if str(validated) != path:
                    raise ValueError(f"media path changed before indexing: {path}")
                stat = validated.stat()
                if (stat.st_size, stat.st_mtime_ns) != (item["size"], item["mtime_ns"]):
                    raise ValueError(f"media changed before indexing: {path}")
            current = {
                (row["channel_number"], row["kind"], row["pool_key"], row["path"])
                for row in connection.execute(
                    """SELECT cm.*, m.path FROM channel_media cm
                       JOIN media_items m ON m.id=cm.media_id"""
                )
            }
            desired = {(number, kind, key, str(path)) for number, kind, key, path in assignments}
            changed = connection.execute(
                "SELECT path, size, mtime_ns FROM media_items"
            ).fetchall()
            signatures = {row["path"]: (row["size"], row["mtime_ns"]) for row in changed}
            if not rebuild_schedule and current == desired and all(
                signatures.get(path) == (item["size"], item["mtime_ns"])
                for path, item in items.items()
            ):
                return len(items)
            self.database.remember_programs(connection, now)
            connection.execute("DELETE FROM channel_media")
            for item in items.values():
                connection.execute(
                    """
                    INSERT INTO media_items
                        (path, kind, duration, container, video_codec, audio_codec,
                         size, mtime_ns, indexed_at)
                    VALUES (:path, :kind, :duration, :container, :video_codec, :audio_codec,
                            :size, :mtime_ns, :indexed_at)
                    ON CONFLICT(path) DO UPDATE SET
                        duration=excluded.duration, container=excluded.container,
                        video_codec=excluded.video_codec, audio_codec=excluded.audio_codec,
                        size=excluded.size, mtime_ns=excluded.mtime_ns, indexed_at=excluded.indexed_at
                    """, item,
                )
            ids = {row["path"]: row["id"] for row in connection.execute("SELECT id, path FROM media_items")}
            connection.executemany(
                """INSERT OR IGNORE INTO channel_media
                   (channel_number, media_id, kind, pool_key) VALUES (?, ?, ?, ?)""",
                [(number, ids[str(path)], kind, key) for number, kind, key, path in assignments],
            )
            connection.execute("DELETE FROM programs WHERE starts_at > ?", (now,))
            connection.execute(
                """DELETE FROM programs WHERE ends_at > ? AND NOT EXISTS (
                       SELECT 1 FROM channel_media cm
                       WHERE cm.channel_number=programs.channel_number
                         AND cm.media_id=programs.media_id
                   )""", (now,),
            )
            connection.execute(
                """DELETE FROM media_items WHERE id NOT IN (SELECT media_id FROM channel_media)
                   AND id NOT IN (SELECT media_id FROM programs)
"""
            )
        return len(items)
