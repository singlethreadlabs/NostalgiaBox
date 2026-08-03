"""FastAPI application for headless NostalgiaBox."""

from __future__ import annotations

import asyncio
import shutil
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from nostalgiabox.config import Config, load_config

from .analytics import AnalyticsStore, RETENTION_SECONDS
from .database import Database
from .media import MediaIndexer
from .playback import PlaybackManager
from .schedule import Program, Scheduler
from .settings import ServerSettings


class PlaybackRequest(BaseModel):
    channel_number: int
    client_type: Literal["browser", "fire_tv"]


class ActivityRequest(BaseModel):
    playing: bool


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
        self.analytics = AnalyticsStore(
            self.database, self.config.schedule_timezone
        )
        self.analytics.cleanup(inactivity_seconds=settings.session_ttl_seconds)
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
                state.analytics.cleanup(
                    inactivity_seconds=state.settings.session_ttl_seconds
                )

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
        try:
            server.analytics.start_session(session.id, program, body.client_type, now)
        except Exception:
            server.playback.release(session.id)
            raise
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
        server = state(request)
        if not server.playback.release(session_id):
            raise HTTPException(status_code=404, detail="playback session not found")
        server.analytics.finish_session(session_id, time.time())
        return Response(status_code=204)

    @app.post("/api/v1/playback-sessions/{session_id}/activity", status_code=204)
    def playback_activity(
        session_id: str, body: ActivityRequest, request: Request
    ) -> Response:
        server = state(request)
        try:
            # Activity heartbeats also renew direct-play sessions, whose media
            # response does not otherwise pass through PlaybackManager.
            server.playback.get(session_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="playback session not found")
        if not server.analytics.activity(session_id, body.playing, time.time()):
            raise HTTPException(status_code=404, detail="viewing session not found")
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

    def analytics_range(
        from_value: datetime | None,
        to_value: datetime | None,
    ) -> tuple[float, float]:
        now = time.time()
        if from_value is not None and from_value.tzinfo is None:
            raise HTTPException(status_code=422, detail="from must include a timezone")
        if to_value is not None and to_value.tzinfo is None:
            raise HTTPException(status_code=422, detail="to must include a timezone")
        start = from_value.timestamp() if from_value else now - RETENTION_SECONDS
        end = to_value.timestamp() if to_value else now
        if start >= end:
            raise HTTPException(status_code=422, detail="from must be before to")
        if end - start > RETENTION_SECONDS:
            raise HTTPException(
                status_code=422, detail="analytics range cannot exceed 365 days"
            )
        return start, end

    @app.get("/api/v1/analytics/summary")
    def analytics_summary(
        request: Request,
        from_value: datetime | None = Query(default=None, alias="from"),
        to_value: datetime | None = Query(default=None, alias="to"),
        client_type: Literal["browser", "fire_tv"] | None = None,
    ) -> dict:
        server = state(request)
        start, end = analytics_range(from_value, to_value)
        return server.analytics.summary(start, end, client_type)

    @app.get("/api/v1/analytics/history")
    def analytics_history(
        request: Request,
        from_value: datetime | None = Query(default=None, alias="from"),
        to_value: datetime | None = Query(default=None, alias="to"),
        client_type: Literal["browser", "fire_tv"] | None = None,
        limit: int = Query(default=25, ge=1, le=100),
        cursor: str | None = None,
    ) -> dict:
        server = state(request)
        start, end = analytics_range(from_value, to_value)
        parsed_cursor = None
        if cursor:
            try:
                timestamp, session_id = cursor.split(":", 1)
                parsed_cursor = (float(timestamp), session_id)
                if not session_id:
                    raise ValueError
            except ValueError:
                raise HTTPException(status_code=422, detail="invalid history cursor")
        return server.analytics.history(
            start, end, client_type, limit, parsed_cursor
        )

    web_dir = Path(__file__).with_name("web")

    @app.get("/analytics", include_in_schema=False)
    @app.get("/analytics/", include_in_schema=False)
    def analytics_page() -> FileResponse:
        return FileResponse(web_dir / "analytics.html")
    app.mount(
        "/fonts",
        StaticFiles(directory=Path(__file__).parents[1] / "assets" / "fonts"),
        name="fonts",
    )
    app.mount("/", StaticFiles(directory=web_dir, html=True), name="web")
    return app


app = create_app()
