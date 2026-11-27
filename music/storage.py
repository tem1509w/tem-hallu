from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, TypeVar

from .models import GuildSettings, GuildStats

T = TypeVar("T")


SCHEMA = """
CREATE TABLE IF NOT EXISTS guild_settings (
    guild_id TEXT PRIMARY KEY,
    auto_247 INTEGER NOT NULL DEFAULT 0,
    voice_channel_id TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS play_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id TEXT NOT NULL,
    user_id TEXT,
    title TEXT NOT NULL,
    artist TEXT NOT NULL DEFAULT '',
    platform TEXT NOT NULL DEFAULT '',
    duration INTEGER,
    played_seconds REAL NOT NULL DEFAULT 0,
    scrobbled INTEGER NOT NULL DEFAULT 0,
    started_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_play_history_guild_started
    ON play_history (guild_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_play_history_guild_scrobbled
    ON play_history (guild_id, scrobbled);
"""


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    """Small async wrapper around a single serialized SQLite connection."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._conn: sqlite3.Connection | None = None
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        if self._conn is not None:
            return
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await asyncio.to_thread(self._open)

    def _open(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript(SCHEMA)
        conn.commit()
        return conn

    async def _run(self, func: Callable[[sqlite3.Connection], T]) -> T:
        if self._conn is None:
            raise RuntimeError("Database chưa được khởi tạo.")
        async with self._lock:
            return await asyncio.to_thread(func, self._conn)

    async def close(self) -> None:
        async with self._lock:
            conn = self._conn
            self._conn = None
            if conn is not None:
                await asyncio.to_thread(conn.close)

    # ------------------------------------------------------------------
    # Guild settings / 24-7
    # ------------------------------------------------------------------
    async def get_settings(self, guild_id: int) -> GuildSettings:
        def query(conn: sqlite3.Connection) -> GuildSettings:
            row = conn.execute(
                "SELECT auto_247, voice_channel_id FROM guild_settings WHERE guild_id = ?",
                (str(guild_id),),
            ).fetchone()
            if row is None:
                return GuildSettings()
            channel = row["voice_channel_id"]
            return GuildSettings(
                auto_247=bool(row["auto_247"]),
                voice_channel_id=int(channel) if channel else None,
            )

        return await self._run(query)

    async def set_247(
        self, guild_id: int, enabled: bool, voice_channel_id: int | None
    ) -> None:
        def execute(conn: sqlite3.Connection) -> None:
            conn.execute(
                """
                INSERT INTO guild_settings (guild_id, auto_247, voice_channel_id, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(guild_id) DO UPDATE SET
                    auto_247 = excluded.auto_247,
                    voice_channel_id = excluded.voice_channel_id,
                    updated_at = excluded.updated_at
                """,
                (
                    str(guild_id),
                    int(enabled),
                    str(voice_channel_id) if voice_channel_id else None,
                    utc_now_iso(),
                ),
            )
            conn.commit()

        await self._run(execute)

    async def update_247_channel(self, guild_id: int, voice_channel_id: int) -> None:
        def execute(conn: sqlite3.Connection) -> None:
            conn.execute(
                """
                UPDATE guild_settings
                SET voice_channel_id = ?, updated_at = ?
                WHERE guild_id = ? AND auto_247 = 1
                """,
                (str(voice_channel_id), utc_now_iso(), str(guild_id)),
            )
            conn.commit()

        await self._run(execute)

    async def list_enabled_247(self) -> list[tuple[int, int]]:
        def query(conn: sqlite3.Connection) -> list[tuple[int, int]]:
            rows = conn.execute(
                """
                SELECT guild_id, voice_channel_id
                FROM guild_settings
                WHERE auto_247 = 1 AND voice_channel_id IS NOT NULL
                """
            ).fetchall()
            return [
                (int(row["guild_id"]), int(row["voice_channel_id"]))
                for row in rows
            ]

        return await self._run(query)

    # ------------------------------------------------------------------
    # Play history / local counters
    # ------------------------------------------------------------------
    async def add_history(
        self,
        guild_id: int,
        user_id: int | None,
        title: str,
        artist: str,
        platform: str,
        duration: int | None,
    ) -> int:
        def execute(conn: sqlite3.Connection) -> int:
            cursor = conn.execute(
                """
                INSERT INTO play_history
                    (guild_id, user_id, title, artist, platform, duration, started_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(guild_id),
                    str(user_id) if user_id else None,
                    title,
                    artist,
                    platform,
                    int(duration) if duration is not None else None,
                    utc_now_iso(),
                ),
            )
            conn.commit()
            return int(cursor.lastrowid)

        return await self._run(execute)

    async def finish_history(
        self, history_id: int | None, played_seconds: float, scrobbled: bool
    ) -> None:
        if history_id is None:
            return

        def execute(conn: sqlite3.Connection) -> None:
            conn.execute(
                """
                UPDATE play_history
                SET played_seconds = ?, scrobbled = ?
                WHERE id = ?
                """,
                (max(0.0, float(played_seconds)), int(scrobbled), history_id),
            )
            conn.commit()

        await self._run(execute)

    async def mark_scrobbled(self, history_id: int | None) -> None:
        if history_id is None:
            return

        def execute(conn: sqlite3.Connection) -> None:
            conn.execute(
                "UPDATE play_history SET scrobbled = 1 WHERE id = ?",
                (history_id,),
            )
            conn.commit()

        await self._run(execute)

    async def get_guild_stats(self, guild_id: int, top_limit: int = 5) -> GuildStats:
        def query_counts(conn: sqlite3.Connection) -> tuple[int, int, float]:
            row = conn.execute(
                """
                SELECT
                    COUNT(*) AS total_plays,
                    COALESCE(SUM(scrobbled), 0) AS scrobbled_plays,
                    COALESCE(SUM(played_seconds), 0) AS listen_seconds
                FROM play_history
                WHERE guild_id = ?
                """,
                (str(guild_id),),
            ).fetchone()
            if row is None:
                return 0, 0, 0.0
            return (
                int(row["total_plays"]),
                int(row["scrobbled_plays"]),
                float(row["listen_seconds"]),
            )

        def query_top(conn: sqlite3.Connection) -> list[dict[str, Any]]:
            rows = conn.execute(
                """
                SELECT title, artist, platform, COUNT(*) AS plays,
                       COALESCE(SUM(played_seconds), 0) AS listen_seconds
                FROM play_history
                WHERE guild_id = ?
                GROUP BY title, artist, platform
                ORDER BY plays DESC, MAX(started_at) DESC
                LIMIT ?
                """,
                (str(guild_id), top_limit),
            ).fetchall()
            return [dict(row) for row in rows]

        total, scrobbled, listen = await self._run(query_counts)
        top = await self._run(query_top)
        return GuildStats(
            total_plays=total,
            scrobbled_plays=scrobbled,
            listen_seconds=listen,
            top_tracks=top,
        )

    async def recent_history(
        self, guild_id: int, limit: int = 10
    ) -> list[dict[str, Any]]:
        def query(conn: sqlite3.Connection) -> list[dict[str, Any]]:
            rows = conn.execute(
                """
                SELECT title, artist, platform, played_seconds, scrobbled, started_at
                FROM play_history
                WHERE guild_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (str(guild_id), max(1, min(limit, 50))),
            ).fetchall()
            return [dict(row) for row in rows]

        return await self._run(query)
