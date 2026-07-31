"""Media indexing and browser playback capability classification."""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

from nostalgiabox.channel import scan_episodes
from nostalgiabox.config import Config
from nostalgiabox.probe import DEFAULT_EPISODE_SECONDS

from .database import Database


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
        return MediaProbe(DEFAULT_EPISODE_SECONDS, None, None, None)
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
    duration = float(duration_raw) if duration_raw else DEFAULT_EPISODE_SECONDS
    if duration <= 0:
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
        return sorted(files, key=lambda path: str(path).lower())

    def refresh(self) -> int:
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

        if not assignments:
            raise ValueError("no playable media found in configured channel pools")

        now = time.time()
        active_paths = {str(path.resolve()) for _, _, _, path in assignments}
        with self.database.connect() as connection:
            connection.execute("DELETE FROM channel_media")
            existing = {
                row["path"]: row
                for row in connection.execute("SELECT * FROM media_items")
            }
            ids: dict[str, int] = {}
            for _, kind, _, path in assignments:
                resolved = str(path.resolve())
                stat = path.stat()
                row = existing.get(resolved)
                if (
                    row is not None
                    and row["size"] == stat.st_size
                    and row["mtime_ns"] == stat.st_mtime_ns
                ):
                    media_id = int(row["id"])
                else:
                    probe = probe_media(path)
                    connection.execute(
                        """
                        INSERT INTO media_items
                            (path, kind, duration, container, video_codec, audio_codec,
                             size, mtime_ns, indexed_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(path) DO UPDATE SET
                            kind=excluded.kind, duration=excluded.duration,
                            container=excluded.container,
                            video_codec=excluded.video_codec,
                            audio_codec=excluded.audio_codec, size=excluded.size,
                            mtime_ns=excluded.mtime_ns, indexed_at=excluded.indexed_at
                        """,
                        (
                            resolved,
                            kind,
                            probe.duration,
                            probe.container,
                            probe.video_codec,
                            probe.audio_codec,
                            stat.st_size,
                            stat.st_mtime_ns,
                            now,
                        ),
                    )
                    media_id = int(
                        connection.execute(
                            "SELECT id FROM media_items WHERE path=?", (resolved,)
                        ).fetchone()["id"]
                    )
                ids[resolved] = media_id

            for channel_number, kind, pool_key, path in assignments:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO channel_media
                        (channel_number, media_id, kind, pool_key)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        channel_number,
                        ids[str(path.resolve())],
                        kind,
                        pool_key,
                    ),
                )

            # Preserve the currently airing item when it still belongs to the
            # channel, but rebuild upcoming programming so new media can enter
            # the rotation. If the current file was removed, drop it as well.
            connection.execute("DELETE FROM programs WHERE starts_at > ?", (now,))
            connection.execute(
                """
                DELETE FROM programs
                WHERE ends_at > ?
                  AND NOT EXISTS (
                    SELECT 1 FROM channel_media cm
                    WHERE cm.channel_number=programs.channel_number
                      AND cm.media_id=programs.media_id
                  )
                """,
                (now,),
            )
            stale = set(existing) - active_paths
            if stale:
                placeholders = ",".join("?" for _ in stale)
                connection.execute(
                    f"""
                    DELETE FROM media_items
                    WHERE path IN ({placeholders})
                      AND id NOT IN (SELECT media_id FROM programs)
                    """,
                    tuple(stale),
                )
        return len(active_paths)
