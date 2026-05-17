"""SQLite-backed persistence for chat sessions and messages.

All public functions are coroutines that wrap blocking sqlite3 calls via
``asyncio.to_thread`` so the bot's event loop never blocks on disk I/O.
"""
from __future__ import annotations

import asyncio
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    title       TEXT    NOT NULL DEFAULT '',
    model       TEXT    NOT NULL,
    created_at  TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  INTEGER NOT NULL,
    role        TEXT    NOT NULL,
    content     TEXT    NOT NULL,
    created_at  TEXT    NOT NULL,
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_sessions_user
    ON sessions(user_id, updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_messages_session
    ON messages(session_id, id);
"""


@dataclass(frozen=True)
class Session:
    id: int
    user_id: int
    title: str
    model: str
    created_at: str
    updated_at: str
    message_count: int


@dataclass(frozen=True)
class Message:
    role: str
    content: str


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def _connect(path: Path) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(str(path))
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        yield conn
        conn.commit()
    finally:
        conn.close()


def _init_db_sync(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with _connect(path) as conn:
        conn.executescript(_SCHEMA)


async def init_db(path: str | Path) -> None:
    """Create tables if they don't exist."""
    await asyncio.to_thread(_init_db_sync, Path(path))


def _create_session_sync(path: Path, user_id: int, model: str) -> int:
    now = _now()
    with _connect(path) as conn:
        cur = conn.execute(
            "INSERT INTO sessions (user_id, title, model, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (user_id, "", model, now, now),
        )
        return int(cur.lastrowid)


async def create_session(path: str | Path, user_id: int, model: str) -> int:
    return await asyncio.to_thread(_create_session_sync, Path(path), user_id, model)


def _get_session_sync(path: Path, session_id: int) -> Session | None:
    with _connect(path) as conn:
        row = conn.execute(
            "SELECT s.id, s.user_id, s.title, s.model, s.created_at, s.updated_at, "
            "       (SELECT COUNT(*) FROM messages m WHERE m.session_id = s.id) AS cnt "
            "FROM sessions s WHERE s.id = ?",
            (session_id,),
        ).fetchone()
    if row is None:
        return None
    return Session(
        id=row["id"],
        user_id=row["user_id"],
        title=row["title"],
        model=row["model"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        message_count=row["cnt"],
    )


async def get_session(path: str | Path, session_id: int) -> Session | None:
    return await asyncio.to_thread(_get_session_sync, Path(path), session_id)


def _list_sessions_sync(path: Path, user_id: int, limit: int) -> list[Session]:
    with _connect(path) as conn:
        rows = conn.execute(
            "SELECT s.id, s.user_id, s.title, s.model, s.created_at, s.updated_at, "
            "       (SELECT COUNT(*) FROM messages m WHERE m.session_id = s.id) AS cnt "
            "FROM sessions s "
            "WHERE s.user_id = ? "
            "ORDER BY s.updated_at DESC "
            "LIMIT ?",
            (user_id, limit),
        ).fetchall()
    return [
        Session(
            id=r["id"],
            user_id=r["user_id"],
            title=r["title"],
            model=r["model"],
            created_at=r["created_at"],
            updated_at=r["updated_at"],
            message_count=r["cnt"],
        )
        for r in rows
    ]


async def list_sessions(
    path: str | Path, user_id: int, limit: int = 20
) -> list[Session]:
    return await asyncio.to_thread(_list_sessions_sync, Path(path), user_id, limit)


def _delete_session_sync(path: Path, session_id: int) -> None:
    with _connect(path) as conn:
        conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
        conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))


async def delete_session(path: str | Path, session_id: int) -> None:
    await asyncio.to_thread(_delete_session_sync, Path(path), session_id)


def _update_session_title_sync(path: Path, session_id: int, title: str) -> None:
    with _connect(path) as conn:
        conn.execute(
            "UPDATE sessions SET title = ?, updated_at = ? WHERE id = ?",
            (title, _now(), session_id),
        )


async def update_session_title(
    path: str | Path, session_id: int, title: str
) -> None:
    await asyncio.to_thread(_update_session_title_sync, Path(path), session_id, title)


def _touch_session_sync(path: Path, session_id: int) -> None:
    with _connect(path) as conn:
        conn.execute(
            "UPDATE sessions SET updated_at = ? WHERE id = ?", (_now(), session_id)
        )


async def touch_session(path: str | Path, session_id: int) -> None:
    await asyncio.to_thread(_touch_session_sync, Path(path), session_id)


def _add_message_sync(
    path: Path, session_id: int, role: str, content: str
) -> None:
    now = _now()
    with _connect(path) as conn:
        conn.execute(
            "INSERT INTO messages (session_id, role, content, created_at) "
            "VALUES (?, ?, ?, ?)",
            (session_id, role, content, now),
        )
        conn.execute(
            "UPDATE sessions SET updated_at = ? WHERE id = ?", (now, session_id)
        )


async def add_message(
    path: str | Path, session_id: int, role: str, content: str
) -> None:
    await asyncio.to_thread(_add_message_sync, Path(path), session_id, role, content)


def _get_messages_sync(path: Path, session_id: int, limit: int) -> list[Message]:
    with _connect(path) as conn:
        # Take the most recent `limit` messages, then return them in chronological order.
        rows = conn.execute(
            "SELECT role, content FROM messages "
            "WHERE session_id = ? "
            "ORDER BY id DESC LIMIT ?",
            (session_id, limit),
        ).fetchall()
    rows.reverse()
    return [Message(role=r["role"], content=r["content"]) for r in rows]


async def get_messages(
    path: str | Path, session_id: int, limit: int = 20
) -> list[Message]:
    return await asyncio.to_thread(_get_messages_sync, Path(path), session_id, limit)
