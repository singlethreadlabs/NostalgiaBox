"""FastAPI application for headless NostalgiaBox."""

from __future__ import annotations

import asyncio
import shutil
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from nostalgiabox.config import Config, load_config

from .database import Database
from .media import MediaIndexer
from .playback import PlaybackManager
from .schedule import Program, Scheduler
from .settings import ServerSettings


class PlaybackRequest(BaseModel):
    channel_number: int


class ServerState:
    def __init__(self, settings: ServerSettings) -> None:
        self.settings = settings
        self.config: Config = load_config(settings.config_path)
        self.database = Database(settings.database_path)
        self.database.initialize()
        self.indexer = MediaIndexer(
            self.database, self.config, settings.media_root
        )
        self.indexed_count = self.indexer.refresh()
        self.scheduler = Scheduler(self.database, self.config)
        self.scheduler.fill()
        self.playback = PlaybackManager(
            settings.cache_dir, settings.session_ttl_seconds
        )
        self.started_at = time.time()

    def refresh(self) -> int:
        count = self.indexer.refresh()
        self.scheduler.fill()
        self.indexed_count = count
        return count


def _program_payload(program: Program, now: float) -> dict:
    return {
        "channel": {
            "number": program.channel_number,
            "name": program.channel_name,
        },
        "program": program.at(now),
    }


def create_app(settings: ServerSettings | None = None) -> FastAPI:
    resolved_settings = settings or ServerSettings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        state = ServerState(resolved_settings)
        app.state.server = state

        async def cleanup() -> None:
            while True:
                await asyncio.sleep(15)
                state.playback.cleanup()

        task = asyncio.create_task(cleanup())
        try:
            yield
        finally:
            task.cancel()
            state.playback.close()

    app = FastAPI(
        title="NostalgiaBox",
        version="1.0.0",
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        lifespan=lifespan,
    )

    def state(request: Request) -> ServerState:
        return request.app.state.server

    @app.get("/api/v1/health")
    def health(request: Request) -> dict:
        server = state(request)
        return {
            "status": "ready",
            "indexed_media": server.indexed_count,
            "channels": len(server.config.channels),
            "ffmpeg": shutil.which("ffmpeg") is not None,
            "ffprobe": shutil.which("ffprobe") is not None,
            "scheduler": "ready",
            "active_sessions": server.playback.active_session_count,
            "active_ffmpeg_processes": server.playback.active_process_count,
            "uptime_seconds": time.time() - server.started_at,
        }

    @app.get("/api/v1/channels")
    def channels(request: Request) -> dict:
        server = state(request)
        now = time.time()
        return {
            "start_channel": server.config.start_channel
            or min(server.config.channel_numbers()),
            "channels": [
                _program_payload(program, now)
                for _, _, program in server.scheduler.channels(now)
            ],
        }

    @app.get("/api/v1/channels/{channel_number}/now")
    def channel_now(channel_number: int, request: Request) -> dict:
        try:
            program = state(request).scheduler.now(channel_number)
        except KeyError:
            raise HTTPException(status_code=404, detail="channel not found")
        return _program_payload(program, time.time())

    @app.post("/api/v1/playback-sessions", status_code=201)
    def create_playback(body: PlaybackRequest, request: Request) -> dict:
        server = state(request)
        now = time.time()
        try:
            program = server.scheduler.now(body.channel_number, now)
        except KeyError:
            raise HTTPException(status_code=404, detail="channel not found")
        session = server.playback.create(program, now)
        if session.error:
            server.playback.release(session.id)
            raise HTTPException(status_code=503, detail=session.error)
        direct = session.delivery_mode == "direct"
        return {
            "id": session.id,
            **_program_payload(program, now),
            "initial_offset_seconds": session.offset,
            "delivery_mode": session.delivery_mode,
            "media_url": (
                f"/api/v1/media/{program.media_id}"
                if direct
                else f"/api/v1/playback-sessions/{session.id}/stream.mp4"
            ),
            # Desktop browsers use a fragmented MP4 stream positioned by
            # FFmpeg. This avoids browsers stalling while byte-range seeking
            # deep into large source MP4 files.
            "browser_url": (
                f"/api/v1/playback-sessions/{session.id}/stream.mp4"
            ),
            "hls_url": (
                None
                if direct
                else f"/api/v1/playback-sessions/{session.id}/hls/index.m3u8"
            ),
            "expires_at": session.expires_at,
        }

    @app.delete("/api/v1/playback-sessions/{session_id}", status_code=204)
    def delete_playback(session_id: str, request: Request) -> Response:
        if not state(request).playback.release(session_id):
            raise HTTPException(status_code=404, detail="playback session not found")
        return Response(status_code=204)

    @app.get("/api/v1/media/{media_id}")
    def direct_media(media_id: int, request: Request):
        server = state(request)
        with server.database.connect() as connection:
            row = connection.execute(
                "SELECT path FROM media_items WHERE id=?", (media_id,)
            ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="media not found")
        path = Path(row["path"]).resolve()
        if not path.is_relative_to(server.settings.media_root.resolve()):
            raise HTTPException(status_code=403, detail="media path rejected")
        return FileResponse(path, media_type="video/mp4")

    @app.get("/api/v1/playback-sessions/{session_id}/stream.mp4")
    def fragmented_mp4(session_id: str, request: Request):
        server = state(request)
        try:
            stream = server.playback.fragmented_mp4(session_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="playback session not found")
        return StreamingResponse(stream, media_type="video/mp4")

    @app.get("/api/v1/playback-sessions/{session_id}/hls/{filename}")
    def hls_file(session_id: str, filename: str, request: Request):
        if "/" in filename or filename.startswith("."):
            raise HTTPException(status_code=400, detail="invalid HLS filename")
        server = state(request)
        try:
            session = server.playback.ensure_hls(session_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="playback session not found")
        path = session.directory / filename
        deadline = time.monotonic() + 10
        while not path.is_file() and time.monotonic() < deadline:
            if session.process is not None and session.process.poll() is not None:
                raise HTTPException(status_code=502, detail="FFmpeg stream failed")
            time.sleep(0.05)
        if not path.is_file():
            raise HTTPException(status_code=504, detail="stream is not ready")
        media_type = (
            "application/vnd.apple.mpegurl"
            if filename.endswith(".m3u8")
            else "video/mp2t"
        )
        return FileResponse(path, media_type=media_type)

    @app.post("/api/v1/admin/refresh")
    def refresh(request: Request) -> dict:
        try:
            count = state(request).refresh()
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        return {"status": "refreshed", "indexed_media": count}

    web_dir = Path(__file__).with_name("web")
    app.mount(
        "/fonts",
        StaticFiles(directory=Path(__file__).parents[1] / "assets" / "fonts"),
        name="fonts",
    )
    app.mount("/", StaticFiles(directory=web_dir, html=True), name="web")
    return app


app = create_app()
