"""Leased adaptive playback sessions backed by FFmpeg."""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Optional

from .schedule import Program


log = logging.getLogger(__name__)
_SESSION_DIRECTORY = re.compile(r"^[0-9a-f]{32}$")


@dataclass
class PlaybackSession:
    id: str
    program: Program
    offset: float
    delivery_mode: str
    expires_at: float
    directory: Path
    process: Optional[subprocess.Popen] = None
    error: Optional[str] = None
    active_streams: int = 0
    stream_processes: list[subprocess.Popen] = field(default_factory=list)
    intentional_process_stops: set[int] = field(default_factory=set)


class PlaybackManager:
    def __init__(self, cache_dir: Path, ttl_seconds: int = 300) -> None:
        self.cache_dir = cache_dir
        self.ttl_seconds = ttl_seconds
        self._sessions: dict[str, PlaybackSession] = {}
        self._lock = threading.Lock()
        cache_dir.mkdir(parents=True, exist_ok=True)
        self._cleanup_abandoned_sessions()

    @property
    def active_session_count(self) -> int:
        with self._lock:
            return len(self._sessions)

    @property
    def active_process_count(self) -> int:
        with self._lock:
            return sum(
                process.poll() is None
                for session in self._sessions.values()
                for process in [session.process, *session.stream_processes]
                if process is not None
            )

    def _cleanup_abandoned_sessions(self) -> None:
        for child in self.cache_dir.iterdir():
            if child.is_dir() and _SESSION_DIRECTORY.fullmatch(child.name):
                shutil.rmtree(child, ignore_errors=True)

    @property
    def available(self) -> bool:
        return shutil.which("ffmpeg") is not None

    def create(self, program: Program, now: Optional[float] = None) -> PlaybackSession:
        now = time.time() if now is None else now
        session_id = uuid.uuid4().hex
        directory = self.cache_dir / session_id
        directory.mkdir(mode=0o700)
        session = PlaybackSession(
            id=session_id,
            program=program,
            offset=max(0.0, now - program.starts_at),
            delivery_mode=program.delivery_mode,
            expires_at=now + self.ttl_seconds,
            directory=directory,
        )
        if program.delivery_mode != "direct":
            if not self.available:
                session.error = "FFmpeg is required for this media"
        with self._lock:
            self._sessions[session_id] = session
        return session

    def get(self, session_id: str, *, touch: bool = True) -> PlaybackSession:
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                raise KeyError(session_id)
            if session.expires_at <= time.time():
                raise KeyError(session_id)
            if touch:
                session.expires_at = time.time() + self.ttl_seconds
            return session

    def release(self, session_id: str) -> bool:
        with self._lock:
            session = self._sessions.pop(session_id, None)
        if session is None:
            return False
        self._stop(session)
        return True

    def cleanup(self) -> None:
        now = time.time()
        with self._lock:
            expired = [
                self._sessions.pop(key)
                for key, session in list(self._sessions.items())
                if session.expires_at <= now and session.active_streams == 0
            ]
        for session in expired:
            self._stop(session)

    def ensure_hls(self, session_id: str) -> PlaybackSession:
        session = self.get(session_id)
        with self._lock:
            if session.process is None:
                session.process = self._start_hls(session)
        return session

    def close(self) -> None:
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            self._stop(session)

    def _start_hls(self, session: PlaybackSession) -> subprocess.Popen:
        output = session.directory / "index.m3u8"
        if session.delivery_mode in {"direct", "remux"}:
            codecs = ["-c", "copy"]
        else:
            codecs = [
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "21",
                "-c:a",
                "aac",
                "-b:a",
                "128k",
            ]
        command = [
            "ffmpeg",
            "-nostdin",
            "-loglevel",
            "warning",
            "-ss",
            f"{session.offset:.3f}",
            "-re",
            "-i",
            str(session.program.path),
            *codecs,
            "-f",
            "hls",
            "-hls_time",
            "4",
            "-hls_list_size",
            "8",
            "-hls_flags",
            "delete_segments+independent_segments",
            "-hls_segment_filename",
            str(session.directory / "segment-%05d.ts"),
            str(output),
        ]
        return self._start_process(session, command, "hls")

    def _start_process(
        self,
        session: PlaybackSession,
        command: list[str],
        purpose: str,
        *,
        stdout=None,
    ) -> subprocess.Popen:
        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.DEVNULL if stdout is None else stdout,
                stderr=subprocess.PIPE,
            )
        except OSError:
            log.exception(
                "FFmpeg failed to start session=%s purpose=%s media=%s",
                session.id,
                purpose,
                session.program.path,
            )
            raise

        errors: deque[str] = deque(maxlen=40)

        def drain_stderr() -> None:
            assert process.stderr is not None
            for raw_line in iter(process.stderr.readline, b""):
                errors.append(raw_line.decode("utf-8", errors="replace")[:2000].rstrip())
            return_code = process.wait()
            if return_code and process.pid not in session.intentional_process_stops:
                detail = " | ".join(line for line in errors if line) or "no stderr"
                log.error(
                    "FFmpeg exited session=%s purpose=%s media=%s code=%s detail=%s",
                    session.id,
                    purpose,
                    session.program.path,
                    return_code,
                    detail,
                )

        threading.Thread(
            target=drain_stderr,
            name=f"ffmpeg-{purpose}-{session.id[:8]}",
            daemon=True,
        ).start()
        return process

    def fragmented_mp4(self, session_id: str) -> Iterator[bytes]:
        session = self.get(session_id)
        if not self.available:
            raise RuntimeError("FFmpeg is unavailable")
        # The desktop-browser stream deliberately transcodes even compatible
        # inputs. Stream-copying from a deep seek can retain source timestamps,
        # leaving Chrome waiting indefinitely for the first decodable frame.
        # Native clients still receive the adaptive direct/HLS endpoints.
        codecs = [
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "21",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
        ]
        process = self._start_process(
            session,
            [
                "ffmpeg",
                "-nostdin",
                "-loglevel",
                "error",
                "-ss",
                f"{session.offset:.3f}",
                "-i",
                str(session.program.path),
                *codecs,
                "-movflags",
                "frag_keyframe+empty_moov+default_base_moof",
                "-f",
                "mp4",
                "pipe:1",
            ],
            "browser",
            stdout=subprocess.PIPE,
        )
        with self._lock:
            session.active_streams += 1
            session.stream_processes.append(process)
        try:
            assert process.stdout is not None
            while chunk := process.stdout.read(64 * 1024):
                with self._lock:
                    session.expires_at = time.time() + self.ttl_seconds
                yield chunk
        finally:
            if process.poll() is None:
                session.intentional_process_stops.add(process.pid)
                process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            with self._lock:
                session.active_streams = max(0, session.active_streams - 1)
                if process in session.stream_processes:
                    session.stream_processes.remove(process)

    @staticmethod
    def _stop(session: PlaybackSession) -> None:
        processes = [session.process, *session.stream_processes]
        for process in (item for item in processes if item is not None):
            if process.poll() is not None:
                continue
            session.intentional_process_stops.add(process.pid)
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        shutil.rmtree(session.directory, ignore_errors=True)
