"""Persistent rolling channel schedules."""

from __future__ import annotations

import hashlib
import random
import re
import threading
import time
from contextlib import nullcontext
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
    show_name: str = ""

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
    def _pick_recent(pool, history, rng: random.Random):
        oldest = min(history.get(row["id"], float("-inf")) for row in pool)
        return rng.choice([
            row for row in pool if history.get(row["id"], float("-inf")) == oldest
        ])

    @staticmethod
    def _pick_show(pool, history, rng: random.Random, ordered_shows):
        by_show: dict[str, list] = {}
        for row in pool:
            by_show.setdefault(row["pool_key"], []).append(row)
        last_show = {
            key: max((history.get(row["id"], float("-inf")) for row in rows))
            for key, rows in by_show.items()
        }
        oldest = min(last_show.values())
        key = rng.choice([key for key, last in last_show.items() if last == oldest])
        episodes = by_show[key]
        if key in ordered_shows:
            episodes.sort(key=lambda row: [
                (0, int(part)) if part.isdigit() else (1, part.casefold())
                for part in re.split(r"(\d+)", row["path"])
            ])
            previous = max(episodes, key=lambda row: history.get(row["id"], float("-inf")))
            if previous["id"] not in history:
                return episodes[0]
            return episodes[(episodes.index(previous) + 1) % len(episodes)]
        return Scheduler._pick_recent(episodes, history, rng)

    def fill(self, now: Optional[float] = None, *, connection=None) -> None:
        now = time.time() if now is None else now
        target = now + self.config.schedule_horizon_hours * 3600
        transaction = self.database.connect() if connection is None else nullcontext(connection)
        with self._lock, transaction as connection:
            self.database.remember_programs(connection, now)
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
                cursor = max(float(last["ends_at"]), now) if last else now
                history = {
                    int(row["media_id"]): float(row["last_started_at"])
                    for row in connection.execute(
                        """
                        SELECT media_id, MAX(last_started_at) AS last_started_at FROM (
                            SELECT media_id, last_started_at FROM selection_history
                            WHERE channel_number=?
                            UNION ALL
                            SELECT media_id, starts_at FROM programs WHERE channel_number=?
                        ) GROUP BY media_id
                        """,
                        (channel.number, channel.number),
                    )
                }
                ordered_shows = {str(path.resolve()) for path in channel.ordered_shows}
                seed = hashlib.sha256(f"{channel.number}:{cursor:.6f}".encode()).digest()
                rng = random.Random(seed)
                initial_show = None
                if last is None and not history:
                    initial_show = self._pick_show(
                        shows, history, rng, ordered_shows
                    )
                    cursor = now - rng.uniform(
                        0,
                        min(float(initial_show["duration"]) * 0.8, 900),
                    )

                while cursor < target:
                    show = initial_show or self._pick_show(shows, history, rng, ordered_shows)
                    sequence = []
                    if last is not None or initial_show is None:
                        if bumpers:
                            sequence.append(("bumper", self._pick_recent(bumpers, history, rng)))
                        if commercials:
                            for _ in range(rng.randint(1, 2)):
                                commercial = self._pick_recent(commercials, history, rng)
                                sequence.append(("commercial", commercial))
                                history[int(commercial["id"])] = cursor
                    sequence.append(("show", show))
                    initial_show = None
                    for kind, row in sequence:
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
                        history[int(row["id"])] = starts_at
            connection.execute("DELETE FROM programs WHERE ends_at < ?", (now - 604800,))

    def now(self, channel_number: int, at: Optional[float] = None) -> Program:
        at = time.time() if at is None else at
        self.fill(at)
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT p.*, m.path, m.container, m.video_codec, m.audio_codec,
                       (SELECT cm.pool_key FROM channel_media cm
                        WHERE cm.channel_number=p.channel_number
                          AND cm.media_id=p.media_id AND cm.kind=p.kind
                        LIMIT 1) AS pool_key
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
            show_name=(
                Path(row["pool_key"]).name
                if row["pool_key"] and row["kind"] == "show"
                else "" if row["kind"] == "show" else row["kind"]
            ),
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
                SELECT p.*, m.path, m.container, m.video_codec, m.audio_codec,
                       (SELECT cm.pool_key FROM channel_media cm
                        WHERE cm.channel_number=p.channel_number
                          AND cm.media_id=p.media_id AND cm.kind=p.kind
                        LIMIT 1) AS pool_key
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
