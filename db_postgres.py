"""PostgreSQL-compatible async database backend for tansaibot (#22).

Drop-in replacement for db.py when using PostgreSQL instead of SQLite.
Uses asyncpg for maximum async performance.

Set env var:
    DATABASE_URL=postgresql://user:pass@host:5432/tansaibot

To switch from SQLite to Postgres:
1. Set DATABASE_URL
2. Run: python db_postgres.py --migrate
3. Change bot.py imports from `db` to `db_postgres as db`

All public functions have the same signature as db.py.
"""
from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DATABASE_URL = os.getenv("DATABASE_URL", "")

try:
    import asyncpg
    _ASYNCPG_AVAILABLE = True
except ImportError:
    _ASYNCPG_AVAILABLE = False
    logger.warning("asyncpg not installed — PostgreSQL backend unavailable. pip install asyncpg")

# Re-export dataclasses from db.py for type compatibility
from db import (
    Session, Message, UserPrefs, SavedPrompt, AuditEntry, Reminder,
    STATUS_ACTIVE, STATUS_WAITLIST, STATUS_BANNED,
)

# ---------------------------------------------------------------------------
# Connection pool
# ---------------------------------------------------------------------------

_pool: "asyncpg.Pool | None" = None


async def get_pool() -> "asyncpg.Pool":
    global _pool
    if _pool is None:
        if not _ASYNCPG_AVAILABLE:
            raise RuntimeError("asyncpg not installed. Run: pip install asyncpg")
        if not DATABASE_URL:
            raise RuntimeError("DATABASE_URL environment variable not set")
        _pool = await asyncpg.create_pool(
            DATABASE_URL,
            min_size=2,
            max_size=20,
            command_timeout=60,
        )
        logger.info("PostgreSQL connection pool created")
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool:
        await _pool.close()
        _pool = None


# ---------------------------------------------------------------------------
# Schema (PostgreSQL DDL)
# ---------------------------------------------------------------------------

_PG_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id                 SERIAL PRIMARY KEY,
    user_id            BIGINT NOT NULL,
    title              TEXT NOT NULL DEFAULT '',
    model              TEXT NOT NULL,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL,
    persona            TEXT NOT NULL DEFAULT '',
    pinned             BOOLEAN NOT NULL DEFAULT FALSE,
    archived           BOOLEAN NOT NULL DEFAULT FALSE,
    summary            TEXT NOT NULL DEFAULT '',
    summary_until_id   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS messages (
    id          SERIAL PRIMARY KEY,
    session_id  INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    role        TEXT NOT NULL,
    content     TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS user_preferences (
    user_id              BIGINT PRIMARY KEY,
    default_model        TEXT NOT NULL DEFAULT '',
    persona              TEXT NOT NULL DEFAULT 'default',
    custom_system_prompt TEXT NOT NULL DEFAULT '',
    ui_language          TEXT NOT NULL DEFAULT 'id',
    onboarded            BOOLEAN NOT NULL DEFAULT FALSE,
    status               TEXT NOT NULL DEFAULT 'active',
    display_name         TEXT NOT NULL DEFAULT '',
    tts_enabled          BOOLEAN NOT NULL DEFAULT FALSE,
    privacy_mode         TEXT NOT NULL DEFAULT 'normal',
    long_term_memory     TEXT NOT NULL DEFAULT '[]',
    tier                 TEXT NOT NULL DEFAULT 'free',
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS usage_log (
    id          SERIAL PRIMARY KEY,
    user_id     BIGINT NOT NULL,
    session_id  INTEGER,
    ts          TEXT NOT NULL,
    model       TEXT NOT NULL,
    tokens_in   INTEGER NOT NULL DEFAULT 0,
    tokens_out  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS saved_prompts (
    id          SERIAL PRIMARY KEY,
    user_id     BIGINT NOT NULL,
    name        TEXT NOT NULL,
    content     TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    UNIQUE (user_id, name)
);

CREATE TABLE IF NOT EXISTS audit_log (
    id          SERIAL PRIMARY KEY,
    admin_id    BIGINT NOT NULL,
    action      TEXT NOT NULL,
    target_id   BIGINT,
    detail      TEXT NOT NULL DEFAULT '',
    ts          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS message_feedback (
    id          SERIAL PRIMARY KEY,
    user_id     BIGINT NOT NULL,
    session_id  INTEGER NOT NULL,
    message_id  INTEGER NOT NULL,
    feedback    TEXT NOT NULL,
    ts          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reminders (
    id          SERIAL PRIMARY KEY,
    user_id     BIGINT NOT NULL,
    chat_id     BIGINT NOT NULL,
    text        TEXT NOT NULL,
    remind_at   TEXT NOT NULL,
    sent        BOOLEAN NOT NULL DEFAULT FALSE,
    created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id, pinned DESC, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, id);
CREATE INDEX IF NOT EXISTS idx_usage_user ON usage_log(user_id, ts);
CREATE INDEX IF NOT EXISTS idx_saved_prompts_user ON saved_prompts(user_id, name);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts DESC);
CREATE INDEX IF NOT EXISTS idx_reminders_user ON reminders(user_id, remind_at);
"""


async def init_db(path: str | Path | None = None) -> None:
    """Initialize PostgreSQL schema (path arg ignored — uses DATABASE_URL)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(_PG_SCHEMA)
    logger.info("PostgreSQL schema initialized")


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Session CRUD (PostgreSQL)
# ---------------------------------------------------------------------------

async def create_session(
    path: str | Path,
    *,
    user_id: int,
    model: str,
    persona: str = "",
) -> int:
    pool = await get_pool()
    now = _now()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO sessions (user_id, model, persona, created_at, updated_at) "
            "VALUES ($1, $2, $3, $4, $4) RETURNING id",
            user_id, model, persona, now,
        )
    return int(row["id"])


async def get_session(path: str | Path, session_id: int) -> Session | None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT s.*, COUNT(m.id) AS message_count FROM sessions s "
            "LEFT JOIN messages m ON m.session_id = s.id "
            "WHERE s.id = $1 GROUP BY s.id",
            session_id,
        )
    if row is None:
        return None
    return Session(
        id=row["id"], user_id=row["user_id"], title=row["title"],
        model=row["model"], created_at=row["created_at"], updated_at=row["updated_at"],
        message_count=int(row["message_count"]), persona=row["persona"] or "",
        pinned=bool(row["pinned"]), archived=bool(row["archived"]),
        summary=row["summary"] or "", summary_until_id=int(row["summary_until_id"]),
    )


async def add_message(path: str | Path, session_id: int, *, role: str, content: str) -> int:
    pool = await get_pool()
    now = _now()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE sessions SET updated_at=$1 WHERE id=$2", now, session_id
        )
        row = await conn.fetchrow(
            "INSERT INTO messages (session_id, role, content, created_at) "
            "VALUES ($1, $2, $3, $4) RETURNING id",
            session_id, role, content, now,
        )
    return int(row["id"])


async def get_messages(path: str | Path, session_id: int, *, limit: int = 50, offset: int = 0) -> list[Message]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, role, content FROM messages WHERE session_id=$1 "
            "ORDER BY id ASC LIMIT $2 OFFSET $3",
            session_id, limit, offset,
        )
    return [Message(role=r["role"], content=r["content"], id=r["id"]) for r in rows]


# ---------------------------------------------------------------------------
# User Prefs CRUD (PostgreSQL)
# ---------------------------------------------------------------------------

async def get_user_prefs(path: str | Path, user_id: int) -> UserPrefs | None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM user_preferences WHERE user_id=$1", user_id
        )
    if row is None:
        return None
    return UserPrefs(
        user_id=int(row["user_id"]),
        default_model=row["default_model"],
        persona=row["persona"],
        custom_system_prompt=row["custom_system_prompt"],
        ui_language=row["ui_language"],
        onboarded=bool(row["onboarded"]),
        status=row["status"],
        display_name=row["display_name"],
        tts_enabled=bool(row["tts_enabled"]),
        privacy_mode=row["privacy_mode"],
        long_term_memory=row["long_term_memory"] or "[]",
        tier=row["tier"] or "free",
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


async def upsert_user_prefs(
    path: str | Path,
    user_id: int,
    *,
    default_model: str | None = None,
    persona: str | None = None,
    custom_system_prompt: str | None = None,
    ui_language: str | None = None,
    onboarded: bool | None = None,
    status: str | None = None,
    display_name: str | None = None,
    tts_enabled: bool | None = None,
    privacy_mode: str | None = None,
    long_term_memory: str | None = None,
    tier: str | None = None,
) -> None:
    pool = await get_pool()
    now = _now()
    async with pool.acquire() as conn:
        existing = await conn.fetchrow(
            "SELECT 1 FROM user_preferences WHERE user_id=$1", user_id
        )
        if existing is None:
            await conn.execute(
                "INSERT INTO user_preferences (user_id, default_model, persona, custom_system_prompt, "
                "ui_language, onboarded, status, display_name, tts_enabled, privacy_mode, "
                "long_term_memory, tier, created_at, updated_at) "
                "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$13)",
                user_id, default_model or "", persona or "default",
                custom_system_prompt or "", ui_language or "id",
                True if onboarded else False, status or STATUS_ACTIVE,
                display_name or "", True if tts_enabled else False,
                privacy_mode or "normal", long_term_memory or "[]", tier or "free", now,
            )
            return

        updates: list[str] = []
        vals: list[Any] = []
        i = 1

        def _add(col: str, val: Any) -> None:
            nonlocal i
            updates.append(f"{col}=${i}")
            vals.append(val)
            i += 1

        if default_model is not None: _add("default_model", default_model)
        if persona is not None: _add("persona", persona)
        if custom_system_prompt is not None: _add("custom_system_prompt", custom_system_prompt)
        if ui_language is not None: _add("ui_language", ui_language)
        if onboarded is not None: _add("onboarded", onboarded)
        if status is not None: _add("status", status)
        if display_name is not None: _add("display_name", display_name)
        if tts_enabled is not None: _add("tts_enabled", tts_enabled)
        if privacy_mode is not None: _add("privacy_mode", privacy_mode)
        if long_term_memory is not None: _add("long_term_memory", long_term_memory)
        if tier is not None: _add("tier", tier)

        if not updates:
            return
        _add("updated_at", now)
        vals.append(user_id)
        await conn.execute(
            f"UPDATE user_preferences SET {', '.join(updates)} WHERE user_id=${i}",
            *vals,
        )


# ---------------------------------------------------------------------------
# Migrations from SQLite → PostgreSQL
# ---------------------------------------------------------------------------

async def migrate_from_sqlite(sqlite_path: str) -> None:
    """Copy all data from a SQLite database into PostgreSQL."""
    import sqlite3
    logger.info("Migrating from SQLite %s → PostgreSQL", sqlite_path)
    conn_sqlite = sqlite3.connect(sqlite_path)
    conn_sqlite.row_factory = sqlite3.Row
    pool = await get_pool()
    await init_db()

    tables = ["sessions", "messages", "user_preferences", "usage_log", "saved_prompts", "audit_log", "message_feedback", "reminders"]
    for table in tables:
        rows = conn_sqlite.execute(f"SELECT * FROM {table}").fetchall()
        if not rows:
            continue
        cols = rows[0].keys()
        placeholders = ", ".join(f"${i+1}" for i in range(len(cols)))
        col_names = ", ".join(cols)
        sql = f"INSERT INTO {table} ({col_names}) VALUES ({placeholders}) ON CONFLICT DO NOTHING"
        async with pool.acquire() as pg_conn:
            for row in rows:
                await pg_conn.execute(sql, *list(row))
        logger.info("Migrated %d rows from %s", len(rows), table)

    conn_sqlite.close()
    logger.info("Migration complete!")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    if "--migrate" in sys.argv:
        sqlite_path = next((a for a in sys.argv if a.endswith(".db")), "chat_history.db")
        asyncio.run(migrate_from_sqlite(sqlite_path))
    elif "--init" in sys.argv:
        asyncio.run(init_db())
        print("PostgreSQL schema initialized.")
    else:
        print("Usage: python db_postgres.py [--init] [--migrate <sqlite.db>]")
