"""Durable active-playback tracking and household analytics queries."""

from __future__ import annotations

import time
from collections import defaultdict
from datetime import datetime, time as datetime_time, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from .database import Database
from .schedule import Program


CLIENT_TYPES = {"browser", "fire_tv"}
MAX_ACTIVITY_INTERVAL_SECONDS = 30.0
RETENTION_SECONDS = 365 * 24 * 60 * 60


class AnalyticsStore:
    def __init__(self, database: Database, timezone: str) -> None:
        self.database = database
        self.timezone = ZoneInfo(timezone)

    def start_session(
        self, session_id: str, program: Program, client_type: str, now: float
    ) -> None:
        if client_type not in CLIENT_TYPES:
            raise ValueError("invalid client type")
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO viewing_sessions (
                    id, program_id, media_id, channel_number, channel_name,
                    show_name, episode_title, media_kind, client_type,
                    started_at, last_activity_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    program.id,
                    program.media_id,
                    program.channel_number,
                    program.channel_name,
                    program.show_name or program.path.parent.name,
                    program.path.stem,
                    program.kind,
                    client_type,
                    now,
                    now,
                ),
            )

    def activity(self, session_id: str, playing: bool, now: float) -> bool:
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM viewing_sessions WHERE id=? AND ended_at IS NULL",
                (session_id,),
            ).fetchone()
            if row is None:
                return False
            self._accrue(connection, row, now)
            connection.execute(
                """
                UPDATE viewing_sessions
                SET is_playing=?, active_since=?, last_activity_at=?
                WHERE id=?
                """,
                (int(playing), now if playing else None, now, session_id),
            )
            return True

    def finish_session(self, session_id: str, now: float) -> bool:
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM viewing_sessions WHERE id=? AND ended_at IS NULL",
                (session_id,),
            ).fetchone()
            if row is None:
                return False
            self._accrue(connection, row, now)
            connection.execute(
                """
                UPDATE viewing_sessions
                SET ended_at=?, is_playing=0, active_since=NULL, last_activity_at=?
                WHERE id=?
                """,
                (now, now, session_id),
            )
            return True

    @staticmethod
    def _accrue(connection, row, now: float) -> None:
        if not row["is_playing"] or row["active_since"] is None:
            return
        started = float(row["active_since"])
        seconds = min(max(0.0, now - started), MAX_ACTIVITY_INTERVAL_SECONDS)
        if seconds <= 0:
            return
        connection.execute(
            """
            INSERT INTO viewing_intervals
                (session_id, started_at, ended_at, watch_seconds)
            VALUES (?, ?, ?, ?)
            """,
            (row["id"], started, started + seconds, seconds),
        )

    def cleanup(self, now: Optional[float] = None, inactivity_seconds: int = 300) -> None:
        now = time.time() if now is None else now
        with self.database.connect() as connection:
            connection.execute(
                """
                UPDATE viewing_sessions
                SET ended_at=last_activity_at, is_playing=0, active_since=NULL
                WHERE ended_at IS NULL AND last_activity_at < ?
                """,
                (now - inactivity_seconds,),
            )
            connection.execute(
                "DELETE FROM viewing_sessions WHERE started_at < ?",
                (now - RETENTION_SECONDS,),
            )

    def summary(
        self, start: float, end: float, client_type: Optional[str] = None
    ) -> dict:
        rows = self._interval_rows(start, end, client_type)
        total = sum(row["seconds"] for row in rows)
        days: dict[str, float] = defaultdict(float)
        channels: dict[tuple[int, str], list[float]] = defaultdict(lambda: [0.0, 0])
        shows: dict[str, list[float]] = defaultdict(lambda: [0.0, 0])
        clients: dict[str, list[float]] = defaultdict(lambda: [0.0, 0])
        sessions: set[str] = set()
        groups: dict[tuple[str, str], set[str]] = defaultdict(set)
        for row in rows:
            seconds = row["seconds"]
            if row["ended_at"] is not None:
                sessions.add(row["id"])
            cursor = row["clipped_start"]
            while cursor < row["clipped_end"]:
                local = datetime.fromtimestamp(cursor, self.timezone)
                next_day = datetime.combine(
                    local.date() + timedelta(days=1), datetime_time(), self.timezone
                ).timestamp()
                portion_end = min(row["clipped_end"], next_day)
                days[local.date().isoformat()] += portion_end - cursor
                cursor = portion_end
            channel_key = (row["channel_number"], row["channel_name"])
            channels[channel_key][0] += seconds
            shows[row["show_name"]][0] += seconds
            clients[row["client_type"]][0] += seconds
            groups[("channel", str(channel_key))].add(row["id"])
            groups[("show", row["show_name"])].add(row["id"])
            groups[("client", row["client_type"])].add(row["id"])

        def ranked(items, kind: str, label):
            result = []
            for key, values in items.items():
                seconds = values[0]
                item = label(key)
                item.update(
                    watch_seconds=seconds,
                    percentage=(seconds / total * 100.0) if total else 0.0,
                    session_count=len(groups[(kind, str(key))]),
                )
                result.append(item)
            return sorted(result, key=lambda item: (-item["watch_seconds"], str(item)))

        first_day = datetime.fromtimestamp(start, self.timezone).date()
        last_day = datetime.fromtimestamp(max(start, end - 0.000001), self.timezone).date()
        day = first_day
        while day <= last_day:
            days.setdefault(day.isoformat(), 0.0)
            day += timedelta(days=1)

        return {
            "from": start,
            "to": end,
            "timezone": str(self.timezone),
            "total_watch_seconds": total,
            "session_count": len(sessions),
            "daily": [
                {"date": day, "watch_seconds": seconds}
                for day, seconds in sorted(days.items())
            ],
            "channels": ranked(
                channels,
                "channel",
                lambda key: {"channel_number": key[0], "channel_name": key[1]},
            ),
            "shows": ranked(
                shows, "show", lambda key: {"show_name": key}
            ),
            "clients": ranked(
                clients, "client", lambda key: {"client_type": key}
            ),
        }

    def _interval_rows(self, start: float, end: float, client_type: Optional[str]):
        client_clause = " AND s.client_type=?" if client_type else ""
        with self.database.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT s.id, s.channel_number, s.channel_name, s.show_name,
                       s.client_type, s.ended_at,
                       MAX(i.started_at, ?) AS clipped_start,
                       MIN(i.ended_at, ?) AS clipped_end,
                       MIN(i.ended_at, ?) - MAX(i.started_at, ?) AS seconds
                FROM viewing_intervals i
                JOIN viewing_sessions s ON s.id=i.session_id
                WHERE s.media_kind='show' AND i.started_at < ? AND i.ended_at > ?
                {client_clause}
                """,
                (start, end, end, start, end, start, *([client_type] if client_type else [])),
            )
            return [dict(row) for row in rows]

    def history(
        self,
        start: float,
        end: float,
        client_type: Optional[str],
        limit: int,
        cursor: Optional[tuple[float, str]],
    ) -> dict:
        clauses = [
            "s.media_kind='show'",
            "i.started_at < ?",
            "i.ended_at > ?",
        ]
        where_params: list[object] = [end, start]
        if client_type:
            clauses.append("s.client_type=?")
            where_params.append(client_type)
        if cursor:
            clauses.append("(s.started_at < ? OR (s.started_at=? AND s.id < ?))")
            where_params.extend((cursor[0], cursor[0], cursor[1]))
        with self.database.connect() as connection:
            rows = list(
                connection.execute(
                    f"""
                    SELECT s.id, s.channel_number, s.channel_name, s.show_name,
                           s.episode_title, s.client_type, s.started_at, s.ended_at,
                           SUM(MIN(i.ended_at, ?) - MAX(i.started_at, ?)) AS watch_seconds
                    FROM viewing_sessions s
                    JOIN viewing_intervals i ON i.session_id=s.id
                    WHERE {' AND '.join(clauses)}
                    GROUP BY s.id
                    ORDER BY s.started_at DESC, s.id DESC
                    LIMIT ?
                    """,
                    (end, start, *where_params, limit + 1),
                )
            )
        has_more = len(rows) > limit
        rows = rows[:limit]
        items = [dict(row) for row in rows]
        next_cursor = None
        if has_more and items:
            last = items[-1]
            next_cursor = f"{last['started_at']}:{last['id']}"
        return {"items": items, "next_cursor": next_cursor}
