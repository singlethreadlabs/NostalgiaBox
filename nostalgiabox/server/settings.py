"""Environment-backed settings for the headless server."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ServerSettings:
    config_path: Path
    database_path: Path
    cache_dir: Path
    media_root: Path
    session_ttl_seconds: int = 300

    @classmethod
    def from_env(cls) -> "ServerSettings":
        return cls(
            config_path=Path(
                os.environ.get("NOSTALGIABOX_CONFIG", "/config/config.yaml")
            ),
            database_path=Path(
                os.environ.get("NOSTALGIABOX_DATABASE", "/data/nostalgiabox.db")
            ),
            cache_dir=Path(
                os.environ.get("NOSTALGIABOX_CACHE", "/cache")
            ),
            media_root=Path(
                os.environ.get("NOSTALGIABOX_MEDIA_ROOT", "/media")
            ),
            session_ttl_seconds=max(
                30, int(os.environ.get("NOSTALGIABOX_SESSION_TTL", "300"))
            ),
        )

