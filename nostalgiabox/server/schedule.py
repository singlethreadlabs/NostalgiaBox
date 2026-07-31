"""Persistent rolling channel schedules."""

from __future__ import annotations

import hashlib
import random
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from nostalgiabox.config import Config

from .database import Database
from .media import MediaProbe


@dataclass(frozen=True)
class Program:
    id: int
    channel_number: int
    channel_name: str
    media_id: int
    kind: str
    path: Path
    starts_at: float
    ends_at: float
    delivery_mode: str

    def at(self, now: float) -> dict:
        return {
            "program_id": self.id,
            "kind": self.kind,
            "title": self.path.stem,
            "starts_at": self.starts_at,
            "ends_at": self.ends_at,
            "elapsed_seconds": max(0.0, min(now - self.starts_at, self.ends_at - self.starts_at)),
            "delivery_mode": self.delivery_mode,
        }


class Scheduler:
    def __init__(self, database: Database, config: Config) -> None:
        self.database = database
        self.config = config
        self._names = {channel.number: channel.name for channel in config.channels}
        self._lock = threading.RLock()

    def _pool(self, connection, channel: int, kind: str):
        return list(
            connection.execute(
                """
                SELECT m.*, cm.pool_key FROM media_items m
                JOIN channel_media cm ON cm.media_id=m.id
                WHERE cm.channel_number=? AND cm.kind=?
                ORDER BY m.path
                """,
                (channel, kind),
            )
        )

    @staticmethod
    def _pick(pool, previous_id: Optional[int], rng: random.Random):
        choices = [row for row in pool if row["id"] != previous_id] or pool
        return rng.choice(choices)

    @classmethod
    def _pick_show(
        cls,
        pool,
        previous_id: Optional[int],
        previous_pool: Optional[str],
        rng: random.Random,
        show_queue: Optional[list[str]] = None,
    ):
        by_show: dict[str, list] = {}
        for row in pool:
            by_show.setdefault(row["pool_key"], []).append(row)
        if show_queue is None:
            show_queue = []
        if not show_queue:
            show_queue.extend(by_show)
            rng.shuffle(show_queue)
            if (
                len(show_queue) > 1
                and previous_pool is not None
                and show_queue[-1] == previous_pool
            ):
                show_queue[0], show_queue[-1] = show_queue[-1], show_queue[0]
        selected_show = show_queue.pop()
        return cls._pick(by_show[selected_show], previous_id, rng)

    def fill(self, now: Optional[float] = None) -> None:
        now = time.time() if now is None else now
        target = now + self.config.schedule_horizon_hours * 3600
        with self._lock, self.database.connect() as connection:
            for channel in self.config.channels:
                shows = self._pool(connection, channel.number, "show")
                if not shows:
                    raise ValueError(f"channel {channel.number} has no show media")
                bumpers = self._pool(connection, channel.number, "bumper")
                commercials = self._pool(connection, channel.number, "commercial")
                last = connection.execute(
                    """
                    SELECT p.ends_at, p.media_id, p.kind
                    FROM programs p
                    WHERE p.channel_number=?
                    ORDER BY p.ends_at DESC LIMIT 1
                    """,
                    (channel.number,),
                ).fetchone()
                cursor = float(last["ends_at"]) if last else now
                previous: dict[str, Optional[int]] = {}
                for kind in ("show", "bumper", "commercial"):
                    previous_row = connection.execute(
                        """
                        SELECT media_id FROM programs
                        WHERE channel_number=? AND kind=?
                        ORDER BY starts_at DESC LIMIT 1
                        """,
                        (channel.number, kind),
                    ).fetchone()
                    previous[kind] = (
                        int(previous_row["media_id"]) if previous_row else None
                    )
                previous_show_row = connection.execute(
                    """
                    SELECT cm.pool_key FROM programs p
                    JOIN channel_media cm
                      ON cm.channel_number=p.channel_number
                     AND cm.media_id=p.media_id
                     AND cm.kind='show'
                    WHERE p.channel_number=? AND p.kind='show'
                    ORDER BY p.starts_at DESC LIMIT 1
                    """,
                    (channel.number,),
                ).fetchone()
                previous_show_pool = (
                    str(previous_show_row["pool_key"])
                    if previous_show_row is not None
                    else None
                )
                seed = hashlib.sha256(
                    f"{channel.number}:{cursor:.6f}".encode()
                ).digest()
                rng = random.Random(seed)
                show_queue: list[str] = []

                initial_show = None
                if last is None:
                    initial_show = self._pick_show(
                        shows, None, None, rng, show_queue
                    )
                    cursor = now - rng.uniform(
                        0,
                        min(float(initial_show["duration"]) * 0.8, 900),
                    )

                while cursor < target:
                    sequence = [("show", shows)]
                    if bumpers:
                        sequence.append(("bumper", bumpers))
                    if commercials:
                        sequence.extend(
                            ("commercial", commercials) for _ in range(rng.randint(1, 2))
                        )
                    for kind, pool in sequence:
                        if kind == "show":
                            row = initial_show or self._pick_show(
                                pool,
                                previous[kind],
                                previous_show_pool,
                                rng,
                                show_queue,
                            )
                            initial_show = None
                            previous_show_pool = str(row["pool_key"])
                        else:
                            row = self._pick(pool, previous[kind], rng)
                        starts_at = cursor
                        cursor += float(row["duration"])
                        connection.execute(
                            """
                            INSERT OR IGNORE INTO programs
                                (channel_number, media_id, kind, starts_at, ends_at)
                            VALUES (?, ?, ?, ?, ?)
                            """,
                            (channel.number, row["id"], kind, starts_at, cursor),
                        )
                        previous[kind] = int(row["id"])
            connection.execute("DELETE FROM programs WHERE ends_at < ?", (now - 604800,))

    def now(self, channel_number: int, at: Optional[float] = None) -> Program:
        at = time.time() if at is None else at
        self.fill(at)
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT p.*, m.path, m.container, m.video_codec, m.audio_codec
                FROM programs p JOIN media_items m ON m.id=p.media_id
                WHERE p.channel_number=? AND p.starts_at<=? AND p.ends_at>?
                ORDER BY p.starts_at DESC LIMIT 1
                """,
                (channel_number, at, at),
            ).fetchone()
        if row is None:
            if channel_number not in self._names:
                raise KeyError(channel_number)
            raise RuntimeError(f"channel {channel_number} has no current program")
        return self._program(row)

    def _program(self, row) -> Program:
        channel_number = int(row["channel_number"])
        probe = MediaProbe(
            duration=float(row["ends_at"] - row["starts_at"]),
            container=row["container"],
            video_codec=row["video_codec"],
            audio_codec=row["audio_codec"],
        )
        return Program(
            id=int(row["id"]),
            channel_number=channel_number,
            channel_name=self._names[channel_number],
            media_id=int(row["media_id"]),
            kind=row["kind"],
            path=Path(row["path"]),
            starts_at=float(row["starts_at"]),
            ends_at=float(row["ends_at"]),
            delivery_mode=probe.delivery_mode,
        )

    def channels(self, at: Optional[float] = None) -> list[tuple[int, str, Program]]:
        at = time.time() if at is None else at
        channel_numbers = self.config.channel_numbers()
        if not channel_numbers:
            return []
        self.fill(at)
        placeholders = ",".join("?" for _ in channel_numbers)
        with self.database.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT p.*, m.path, m.container, m.video_codec, m.audio_codec
                FROM programs p JOIN media_items m ON m.id=p.media_id
                WHERE p.channel_number IN ({placeholders})
                  AND p.starts_at<=? AND p.ends_at>?
                ORDER BY p.channel_number, p.starts_at DESC
                """,
                (*channel_numbers, at, at),
            )
            current = {}
            for row in rows:
                channel_number = int(row["channel_number"])
                if channel_number not in current:
                    current[channel_number] = self._program(row)

        missing = set(channel_numbers) - set(current)
        if missing:
            raise RuntimeError(
                "channels have no current program: "
                + ", ".join(str(number) for number in sorted(missing))
            )
        return [
            (channel.number, channel.name, current[channel.number])
            for channel in sorted(self.config.channels, key=lambda item: item.number)
        ]
