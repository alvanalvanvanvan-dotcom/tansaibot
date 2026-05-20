"""SQLite-backed persistence for chat sessions, messages, and user preferences.

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
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id            INTEGER NOT NULL,
    title              TEXT    NOT NULL DEFAULT '',
    model              TEXT    NOT NULL,
    created_at         TEXT    NOT NULL,
    updated_at         TEXT    NOT NULL,
    persona            TEXT    NOT NULL DEFAULT '',
    pinned             INTEGER NOT NULL DEFAULT 0,
    archived           INTEGER NOT NULL DEFAULT 0,
    summary            TEXT    NOT NULL DEFAULT '',
    summary_until_id   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  INTEGER NOT NULL,
    role        TEXT    NOT NULL,
    content     TEXT    NOT NULL,
    created_at  TEXT    NOT NULL,
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS user_preferences (
    user_id              INTEGER PRIMARY KEY,
    default_model        TEXT NOT NULL DEFAULT '',
    persona              TEXT NOT NULL DEFAULT 'default',
    custom_system_prompt TEXT NOT NULL DEFAULT '',
    ui_language          TEXT NOT NULL DEFAULT 'id',
    onboarded            INTEGER NOT NULL DEFAULT 0,
    status               TEXT NOT NULL DEFAULT 'active',
    display_name         TEXT NOT NULL DEFAULT '',
    tts_enabled          INTEGER NOT NULL DEFAULT 1,
    privacy_mode         TEXT NOT NULL DEFAULT 'normal',
    long_term_memory     TEXT NOT NULL DEFAULT '[]',
    tier                 TEXT NOT NULL DEFAULT 'free',
    rate_limit_minute    INTEGER,
    rate_limit_day       INTEGER,
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS usage_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    session_id  INTEGER,
    ts          TEXT    NOT NULL,
    model       TEXT    NOT NULL,
    tokens_in   INTEGER NOT NULL DEFAULT 0,
    tokens_out  INTEGER NOT NULL DEFAULT 0
);

-- (#8) Saved prompt templates per user
CREATE TABLE IF NOT EXISTS saved_prompts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    name        TEXT    NOT NULL,
    content     TEXT    NOT NULL,
    created_at  TEXT    NOT NULL,
    UNIQUE (user_id, name)
);

-- (#28) Audit log for admin actions
CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    admin_id    INTEGER NOT NULL,
    action      TEXT    NOT NULL,
    target_id   INTEGER,
    detail      TEXT    NOT NULL DEFAULT '',
    ts          TEXT    NOT NULL
);

-- (#7) User reaction feedback on AI messages
CREATE TABLE IF NOT EXISTS message_feedback (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    session_id  INTEGER NOT NULL,
    message_id  INTEGER NOT NULL,
    feedback    TEXT    NOT NULL,
    ts          TEXT    NOT NULL
);

-- (#40) Scheduled reminders
CREATE TABLE IF NOT EXISTS reminders (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    chat_id     INTEGER NOT NULL,
    text        TEXT    NOT NULL,
    remind_at   TEXT    NOT NULL,
    sent        INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sessions_user
    ON sessions(user_id, pinned DESC, updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_messages_session
    ON messages(session_id, id);

CREATE INDEX IF NOT EXISTS idx_usage_user
    ON usage_log(user_id, ts);

CREATE INDEX IF NOT EXISTS idx_saved_prompts_user
    ON saved_prompts(user_id, name);

CREATE INDEX IF NOT EXISTS idx_audit_log_ts
    ON audit_log(ts DESC);

CREATE INDEX IF NOT EXISTS idx_reminders_user
    ON reminders(user_id, remind_at);
"""


# Columns that may be missing on databases created before a given migration.
# (column_name, ddl) — ALTER TABLE ADD COLUMN is run only if absent.
_SESSION_MIGRATIONS: list[tuple[str, str]] = [
    ("persona", "ALTER TABLE sessions ADD COLUMN persona TEXT NOT NULL DEFAULT ''"),
    ("pinned", "ALTER TABLE sessions ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0"),
    ("archived", "ALTER TABLE sessions ADD COLUMN archived INTEGER NOT NULL DEFAULT 0"),
    ("summary", "ALTER TABLE sessions ADD COLUMN summary TEXT NOT NULL DEFAULT ''"),
    (
        "summary_until_id",
        "ALTER TABLE sessions ADD COLUMN summary_until_id INTEGER NOT NULL DEFAULT 0",
    ),
]

_USER_PREFS_MIGRATIONS: list[tuple[str, str]] = [
    (
        "status",
        "ALTER TABLE user_preferences ADD COLUMN status TEXT NOT NULL DEFAULT 'active'",
    ),
    (
        "display_name",
        "ALTER TABLE user_preferences ADD COLUMN display_name TEXT NOT NULL DEFAULT ''",
    ),
    (
        "tts_enabled",
        "ALTER TABLE user_preferences ADD COLUMN tts_enabled INTEGER NOT NULL DEFAULT 0",
    ),
    (
        "privacy_mode",
        "ALTER TABLE user_preferences ADD COLUMN privacy_mode TEXT NOT NULL DEFAULT 'normal'",
    ),
]

# Valid status values for user access.
STATUS_ACTIVE = "active"
STATUS_WAITLIST = "waitlist"
STATUS_BANNED = "banned"


@dataclass(frozen=True)
class Session:
    id: int
    user_id: int
    title: str
    model: str
    created_at: str
    updated_at: str
    message_count: int
    persona: str = ""
    pinned: bool = False
    archived: bool = False
    summary: str = ""
    summary_until_id: int = 0


@dataclass(frozen=True)
class Message:
    role: str
    content: str
    id: int = 0


@dataclass(frozen=True)
class UserPrefs:
    user_id: int
    default_model: str
    persona: str
    custom_system_prompt: str
    ui_language: str
    onboarded: bool
    status: str
    display_name: str
    tts_enabled: bool
    privacy_mode: str
    long_term_memory: str = "[]"  # JSON list of strings (#35)
    tier: str = "free"            # free | premium | admin (#45)
    rate_limit_minute: int | None = None
    rate_limit_day: int | None = None
    created_at: str = ""
    updated_at: str = ""


@dataclass(frozen=True)
class SavedPrompt:
    id: int
    user_id: int
    name: str
    content: str
    created_at: str


@dataclass(frozen=True)
class AuditEntry:
    id: int
    admin_id: int
    action: str
    target_id: int | None
    detail: str
    ts: str


@dataclass(frozen=True)
class UsageTotals:
    messages: int
    tokens_in: int
    tokens_out: int


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


def _existing_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {r["name"] for r in rows}


def _apply_session_migrations(conn: sqlite3.Connection) -> None:
    existing = _existing_columns(conn, "sessions")
    for column, ddl in _SESSION_MIGRATIONS:
        if column not in existing:
            conn.execute(ddl)


def _apply_user_prefs_migrations(conn: sqlite3.Connection) -> None:
    """Migrate user_preferences table by adding new columns safely."""
    # (#35) long term memory
    try:
        conn.execute("ALTER TABLE user_preferences ADD COLUMN long_term_memory TEXT NOT NULL DEFAULT '[]'")
    except sqlite3.OperationalError:
        pass
    # (#45) tier
    try:
        conn.execute("ALTER TABLE user_preferences ADD COLUMN tier TEXT NOT NULL DEFAULT 'free'")
    except sqlite3.OperationalError:
        pass
    # (Tier L) rate limits
    try:
        conn.execute("ALTER TABLE user_preferences ADD COLUMN rate_limit_minute INTEGER")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE user_preferences ADD COLUMN rate_limit_day INTEGER")
    except sqlite3.OperationalError:
        pass
    existing = _existing_columns(conn, "user_preferences")
    for column, ddl in _USER_PREFS_MIGRATIONS:
        if column not in existing:
            conn.execute(ddl)


def _init_db_sync(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with _connect(path) as conn:
        conn.executescript(_SCHEMA)
        _apply_session_migrations(conn)
        _apply_user_prefs_migrations(conn)


async def init_db(path: str | Path) -> None:
    """Create tables (and apply lightweight migrations) if needed."""
    await asyncio.to_thread(_init_db_sync, Path(path))


# --- Sessions ---------------------------------------------------------------

def _create_session_sync(
    path: Path, user_id: int, model: str, persona: str
) -> int:
    now = _now()
    with _connect(path) as conn:
        cur = conn.execute(
            "INSERT INTO sessions (user_id, title, model, created_at, updated_at, persona) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, "", model, now, now, persona),
        )
        return int(cur.lastrowid)


async def create_session(
    path: str | Path, user_id: int, model: str, persona: str = ""
) -> int:
    return await asyncio.to_thread(
        _create_session_sync, Path(path), user_id, model, persona
    )


def _row_to_session(row: sqlite3.Row) -> Session:
    keys = row.keys()
    return Session(
        id=row["id"],
        user_id=row["user_id"],
        title=row["title"],
        model=row["model"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        message_count=row["cnt"] if "cnt" in keys else 0,
        persona=row["persona"] if "persona" in keys else "",
        pinned=bool(row["pinned"]) if "pinned" in keys else False,
        archived=bool(row["archived"]) if "archived" in keys else False,
        summary=row["summary"] if "summary" in keys else "",
        summary_until_id=int(row["summary_until_id"])
        if "summary_until_id" in keys
        else 0,
    )


_SESSION_SELECT = (
    "SELECT s.id, s.user_id, s.title, s.model, s.created_at, s.updated_at, "
    "       s.persona, s.pinned, s.archived, s.summary, s.summary_until_id, "
    "       (SELECT COUNT(*) FROM messages m WHERE m.session_id = s.id) AS cnt "
    "FROM sessions s "
)


def _get_session_sync(path: Path, session_id: int) -> Session | None:
    with _connect(path) as conn:
        row = conn.execute(_SESSION_SELECT + "WHERE s.id = ?", (session_id,)).fetchone()
    return _row_to_session(row) if row is not None else None


async def get_session(path: str | Path, session_id: int) -> Session | None:
    return await asyncio.to_thread(_get_session_sync, Path(path), session_id)


def _list_sessions_sync(
    path: Path,
    user_id: int,
    limit: int,
    offset: int,
    include_archived: bool,
) -> list[Session]:
    where = "WHERE s.user_id = ?"
    params: list[object] = [user_id]
    if not include_archived:
        where += " AND s.archived = 0"
    with _connect(path) as conn:
        rows = conn.execute(
            _SESSION_SELECT
            + where
            + " ORDER BY s.pinned DESC, s.updated_at DESC LIMIT ? OFFSET ?",
            params + [limit, offset],
        ).fetchall()
    return [_row_to_session(r) for r in rows]


async def list_sessions(
    path: str | Path,
    user_id: int,
    limit: int = 20,
    offset: int = 0,
    include_archived: bool = False,
) -> list[Session]:
    return await asyncio.to_thread(
        _list_sessions_sync, Path(path), user_id, limit, offset, include_archived
    )


def _count_sessions_sync(
    path: Path, user_id: int, include_archived: bool
) -> int:
    where = "WHERE user_id = ?"
    params: list[object] = [user_id]
    if not include_archived:
        where += " AND archived = 0"
    with _connect(path) as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM sessions " + where, params
        ).fetchone()
    return int(row["c"]) if row is not None else 0


async def count_sessions(
    path: str | Path, user_id: int, include_archived: bool = False
) -> int:
    return await asyncio.to_thread(
        _count_sessions_sync, Path(path), user_id, include_archived
    )


def _search_sessions_sync(
    path: Path, user_id: int, query: str, limit: int
) -> list[Session]:
    like = f"%{query}%"
    with _connect(path) as conn:
        rows = conn.execute(
            _SESSION_SELECT
            + "WHERE s.user_id = ? AND ("
            "    s.title LIKE ? OR "
            "    EXISTS (SELECT 1 FROM messages m WHERE m.session_id = s.id "
            "            AND m.content LIKE ?))"
            " ORDER BY s.updated_at DESC LIMIT ?",
            (user_id, like, like, limit),
        ).fetchall()
    return [_row_to_session(r) for r in rows]


async def search_sessions(
    path: str | Path, user_id: int, query: str, limit: int = 20
) -> list[Session]:
    return await asyncio.to_thread(
        _search_sessions_sync, Path(path), user_id, query, limit
    )


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
    await asyncio.to_thread(
        _update_session_title_sync, Path(path), session_id, title
    )


def _set_pin_sync(path: Path, session_id: int, pinned: bool) -> None:
    with _connect(path) as conn:
        conn.execute(
            "UPDATE sessions SET pinned = ?, updated_at = ? WHERE id = ?",
            (1 if pinned else 0, _now(), session_id),
        )


async def set_pin(path: str | Path, session_id: int, pinned: bool) -> None:
    await asyncio.to_thread(_set_pin_sync, Path(path), session_id, pinned)


def _set_archive_sync(path: Path, session_id: int, archived: bool) -> None:
    with _connect(path) as conn:
        conn.execute(
            "UPDATE sessions SET archived = ?, updated_at = ? WHERE id = ?",
            (1 if archived else 0, _now(), session_id),
        )


async def set_archive(path: str | Path, session_id: int, archived: bool) -> None:
    await asyncio.to_thread(_set_archive_sync, Path(path), session_id, archived)


def _set_session_persona_sync(path: Path, session_id: int, persona: str) -> None:
    with _connect(path) as conn:
        conn.execute(
            "UPDATE sessions SET persona = ?, updated_at = ? WHERE id = ?",
            (persona, _now(), session_id),
        )


async def set_session_persona(
    path: str | Path, session_id: int, persona: str
) -> None:
    await asyncio.to_thread(
        _set_session_persona_sync, Path(path), session_id, persona
    )


def _touch_session_sync(path: Path, session_id: int) -> None:
    with _connect(path) as conn:
        conn.execute(
            "UPDATE sessions SET updated_at = ? WHERE id = ?", (_now(), session_id)
        )


async def touch_session(path: str | Path, session_id: int) -> None:
    await asyncio.to_thread(_touch_session_sync, Path(path), session_id)


# --- Messages ---------------------------------------------------------------

def _add_message_sync(
    path: Path, session_id: int, role: str, content: str
) -> int:
    now = _now()
    with _connect(path) as conn:
        cur = conn.execute(
            "INSERT INTO messages (session_id, role, content, created_at) "
            "VALUES (?, ?, ?, ?)",
            (session_id, role, content, now),
        )
        conn.execute(
            "UPDATE sessions SET updated_at = ? WHERE id = ?", (now, session_id)
        )
        return int(cur.lastrowid)


async def add_message(
    path: str | Path, session_id: int, role: str, content: str
) -> int:
    return await asyncio.to_thread(
        _add_message_sync, Path(path), session_id, role, content
    )


def _get_messages_sync(path: Path, session_id: int, limit: int) -> list[Message]:
    with _connect(path) as conn:
        rows = conn.execute(
            "SELECT id, role, content FROM messages "
            "WHERE session_id = ? "
            "ORDER BY id DESC LIMIT ?",
            (session_id, limit),
        ).fetchall()
    rows.reverse()
    return [
        Message(id=int(r["id"]), role=r["role"], content=r["content"]) for r in rows
    ]


async def get_messages(
    path: str | Path, session_id: int, limit: int = 20
) -> list[Message]:
    return await asyncio.to_thread(
        _get_messages_sync, Path(path), session_id, limit
    )


def _get_all_messages_sync(path: Path, session_id: int) -> list[Message]:
    with _connect(path) as conn:
        rows = conn.execute(
            "SELECT id, role, content FROM messages "
            "WHERE session_id = ? ORDER BY id ASC",
            (session_id,),
        ).fetchall()
    return [
        Message(id=int(r["id"]), role=r["role"], content=r["content"]) for r in rows
    ]


async def get_all_messages(path: str | Path, session_id: int) -> list[Message]:
    return await asyncio.to_thread(_get_all_messages_sync, Path(path), session_id)


def _delete_last_assistant_message_sync(
    path: Path, session_id: int
) -> tuple[str, str] | None:
    """Delete the most recent assistant turn (and its triggering user message).

    Returns ``(user_content, assistant_content)`` of what was removed, or
    ``None`` if there was no assistant message to delete.
    """
    with _connect(path) as conn:
        row_a = conn.execute(
            "SELECT id, content FROM messages "
            "WHERE session_id = ? AND role = 'assistant' "
            "ORDER BY id DESC LIMIT 1",
            (session_id,),
        ).fetchone()
        if row_a is None:
            return None
        assistant_id = row_a["id"]
        assistant_content = row_a["content"]
        row_u = conn.execute(
            "SELECT id, content FROM messages "
            "WHERE session_id = ? AND role = 'user' AND id < ? "
            "ORDER BY id DESC LIMIT 1",
            (session_id, assistant_id),
        ).fetchone()
        if row_u is None:
            return None
        user_message_id = row_u["id"]
        user_content = row_u["content"]
        conn.execute(
            "DELETE FROM messages WHERE id IN (?, ?)",
            (assistant_id, user_message_id),
        )
        return (user_content, assistant_content)


async def delete_last_assistant_message(
    path: str | Path, session_id: int
) -> tuple[str, str] | None:
    return await asyncio.to_thread(
        _delete_last_assistant_message_sync, Path(path), session_id
    )


def _count_messages_sync(path: Path, session_id: int) -> int:
    with _connect(path) as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM messages WHERE session_id = ?",
            (session_id,),
        ).fetchone()
    return int(row["c"]) if row is not None else 0


async def count_messages(path: str | Path, session_id: int) -> int:
    return await asyncio.to_thread(_count_messages_sync, Path(path), session_id)


def _update_session_summary_sync(
    path: Path, session_id: int, summary: str, summary_until_id: int
) -> None:
    with _connect(path) as conn:
        conn.execute(
            "UPDATE sessions SET summary = ?, summary_until_id = ?, updated_at = ? "
            "WHERE id = ?",
            (summary, summary_until_id, _now(), session_id),
        )


async def update_session_summary(
    path: str | Path, session_id: int, summary: str, summary_until_id: int
) -> None:
    await asyncio.to_thread(
        _update_session_summary_sync,
        Path(path),
        session_id,
        summary,
        summary_until_id,
    )


# --- User preferences -------------------------------------------------------

def _get_user_prefs_sync(path: Path, user_id: int) -> UserPrefs | None:
    with _connect(path) as conn:
        row = conn.execute(
            "SELECT * FROM user_preferences WHERE user_id = ?", (user_id,)
        ).fetchone()
    if row is None:
        return None
    keys = row.keys()
    return UserPrefs(
        user_id=row["user_id"],
        default_model=row["default_model"],
        persona=row["persona"],
        custom_system_prompt=row["custom_system_prompt"],
        ui_language=row["ui_language"],
        onboarded=bool(row["onboarded"]),
        status=row["status"] if "status" in keys else STATUS_ACTIVE,
        display_name=row["display_name"] if "display_name" in keys else "",
        tts_enabled=bool(row["tts_enabled"]) if "tts_enabled" in keys else False,
        privacy_mode=row["privacy_mode"] if "privacy_mode" in keys else "normal",
        long_term_memory=row["long_term_memory"] if "long_term_memory" in keys else "[]",
        tier=row["tier"] if "tier" in keys else "free",
        rate_limit_minute=row["rate_limit_minute"] if "rate_limit_minute" in keys else None,
        rate_limit_day=row["rate_limit_day"] if "rate_limit_day" in keys else None,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


async def get_user_prefs(path: str | Path, user_id: int) -> UserPrefs | None:
    return await asyncio.to_thread(_get_user_prefs_sync, Path(path), user_id)


def _upsert_user_prefs_sync(
    path: Path,
    user_id: int,
    *,
    default_model: str | None,
    persona: str | None,
    custom_system_prompt: str | None,
    ui_language: str | None,
    onboarded: bool | None,
    status: str | None,
    display_name: str | None,
    tts_enabled: bool | None,
    privacy_mode: str | None = None,
    long_term_memory: str | None = None,
    tier: str | None = None,
    rate_limit_minute: int | None = None,
    rate_limit_day: int | None = None,
) -> None:
    ts = _now()
    with _connect(path) as conn:
        existing = conn.execute(
            "SELECT 1 FROM user_preferences WHERE user_id = ?", (user_id,)
        ).fetchone()
        if existing is None:
            conn.execute(
                "INSERT INTO user_preferences ("
                "user_id, default_model, persona, "
                "custom_system_prompt, ui_language, onboarded, status, "
                "display_name, tts_enabled, privacy_mode, long_term_memory, tier, "
                "rate_limit_minute, rate_limit_day, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    user_id,
                    default_model or "",
                    persona or "default",
                    custom_system_prompt or "",
                    ui_language or "id",
                    int(bool(onboarded)),
                    status or STATUS_ACTIVE,
                    display_name or "",
                    int(tts_enabled) if tts_enabled is not None else 1,
                    privacy_mode or "normal",
                    long_term_memory or "[]",
                    tier or "free",
                    rate_limit_minute,
                    rate_limit_day,
                    ts,
                    ts,
                ),
            )
            return
        fields: list[str] = []
        params: list[object] = []
        if default_model is not None:
            fields.append("default_model = ?")
            params.append(default_model)
        if persona is not None:
            fields.append("persona = ?")
            params.append(persona)
        if custom_system_prompt is not None:
            fields.append("custom_system_prompt = ?")
            params.append(custom_system_prompt)
        if ui_language is not None:
            fields.append("ui_language = ?")
            params.append(ui_language)
        if onboarded is not None:
            fields.append("onboarded = ?")
            params.append(1 if onboarded else 0)
        if status is not None:
            fields.append("status = ?")
            params.append(status)
        if display_name is not None:
            fields.append("display_name = ?")
            params.append(display_name)
        if tts_enabled is not None:
            fields.append("tts_enabled = ?")
            params.append(1 if tts_enabled else 0)
        if privacy_mode is not None:
            fields.append("privacy_mode = ?")
            params.append(privacy_mode)
        if long_term_memory is not None:
            fields.append("long_term_memory = ?")
            params.append(long_term_memory)
        if tier is not None:
            fields.append("tier = ?")
            params.append(tier)
        if rate_limit_minute is not None:
            fields.append("rate_limit_minute = ?")
            params.append(rate_limit_minute)
        if rate_limit_day is not None:
            fields.append("rate_limit_day = ?")
            params.append(rate_limit_day)
        
        if not fields:
            return
        fields.append("updated_at = ?")
        params.append(ts)
        params.append(user_id)
        conn.execute(
            f"UPDATE user_preferences SET {', '.join(fields)} WHERE user_id = ?",
            params,
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
    rate_limit_minute: int | None = None,
    rate_limit_day: int | None = None,
) -> None:
    await asyncio.to_thread(
        _upsert_user_prefs_sync,
        Path(path),
        user_id,
        default_model=default_model,
        persona=persona,
        custom_system_prompt=custom_system_prompt,
        ui_language=ui_language,
        onboarded=onboarded,
        status=status,
        display_name=display_name,
        tts_enabled=tts_enabled,
        privacy_mode=privacy_mode,
        long_term_memory=long_term_memory,
        tier=tier,
        rate_limit_minute=rate_limit_minute,
        rate_limit_day=rate_limit_day,
    )


# --- User access status -----------------------------------------------------

def _list_users_by_status_sync(
    path: Path, status: str, limit: int
) -> list[tuple[int, str, str]]:
    with _connect(path) as conn:
        rows = conn.execute(
            "SELECT user_id, display_name, created_at FROM user_preferences "
            "WHERE status = ? ORDER BY created_at ASC LIMIT ?",
            (status, limit),
        ).fetchall()
    return [
        (int(r["user_id"]), r["display_name"] or "", r["created_at"]) for r in rows
    ]


async def list_users_by_status(
    path: str | Path, status: str, limit: int = 200
) -> list[tuple[int, str, str]]:
    return await asyncio.to_thread(
        _list_users_by_status_sync, Path(path), status, limit
    )


def _count_users_by_status_sync(path: Path) -> dict[str, int]:
    with _connect(path) as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS c FROM user_preferences GROUP BY status"
        ).fetchall()
    return {r["status"]: int(r["c"]) for r in rows}


async def count_users_by_status(path: str | Path) -> dict[str, int]:
    return await asyncio.to_thread(_count_users_by_status_sync, Path(path))


def _list_user_ids_sync(path: Path) -> list[int]:
    with _connect(path) as conn:
        rows = conn.execute(
            "SELECT DISTINCT user_id FROM user_preferences"
        ).fetchall()
    return [int(r["user_id"]) for r in rows]


async def list_user_ids(path: str | Path) -> list[int]:
    return await asyncio.to_thread(_list_user_ids_sync, Path(path))


# --- Usage tracking ---------------------------------------------------------

def _log_usage_sync(
    path: Path,
    user_id: int,
    session_id: int | None,
    model: str,
    tokens_in: int,
    tokens_out: int,
) -> None:
    with _connect(path) as conn:
        conn.execute(
            "INSERT INTO usage_log (user_id, session_id, ts, model, tokens_in, "
            "tokens_out) VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, session_id, _now(), model, tokens_in, tokens_out),
        )


async def log_usage(
    path: str | Path,
    user_id: int,
    session_id: int | None,
    model: str,
    tokens_in: int,
    tokens_out: int,
) -> None:
    await asyncio.to_thread(
        _log_usage_sync, Path(path), user_id, session_id, model, tokens_in, tokens_out
    )


def _usage_totals_sync(path: Path, user_id: int) -> UsageTotals:
    with _connect(path) as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS msgs, "
            "       COALESCE(SUM(tokens_in), 0) AS ti, "
            "       COALESCE(SUM(tokens_out), 0) AS too "
            "FROM usage_log WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    if row is None:
        return UsageTotals(0, 0, 0)
    return UsageTotals(
        messages=int(row["msgs"]),
        tokens_in=int(row["ti"]),
        tokens_out=int(row["too"]),
    )


async def usage_totals(path: str | Path, user_id: int) -> UsageTotals:
    return await asyncio.to_thread(_usage_totals_sync, Path(path), user_id)


def _global_stats_sync(path: Path) -> dict[str, int]:
    with _connect(path) as conn:
        users = conn.execute(
            "SELECT COUNT(DISTINCT user_id) AS c FROM user_preferences"
        ).fetchone()
        sessions = conn.execute(
            "SELECT COUNT(*) AS c FROM sessions"
        ).fetchone()
        messages = conn.execute(
            "SELECT COUNT(*) AS c FROM messages"
        ).fetchone()
    return {
        "users": int(users["c"]) if users else 0,
        "sessions": int(sessions["c"]) if sessions else 0,
        "messages": int(messages["c"]) if messages else 0,
    }


async def global_stats(path: str | Path) -> dict[str, int]:
    return await asyncio.to_thread(_global_stats_sync, Path(path))


# --- Saved Prompts (#8) ---------------------------------------------------

def _save_prompt_sync(path: Path, user_id: int, name: str, content: str) -> None:
    with _connect(path) as conn:
        conn.execute(
            "INSERT INTO saved_prompts (user_id, name, content, created_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(user_id, name) DO UPDATE SET content=excluded.content, created_at=excluded.created_at",
            (user_id, name, content, _now()),
        )


async def save_prompt(path: str | Path, user_id: int, name: str, content: str) -> None:
    """Save or overwrite a named prompt template for a user."""
    await asyncio.to_thread(_save_prompt_sync, Path(path), user_id, name, content)


def _list_saved_prompts_sync(path: Path, user_id: int) -> list[SavedPrompt]:
    with _connect(path) as conn:
        rows = conn.execute(
            "SELECT id, user_id, name, content, created_at FROM saved_prompts "
            "WHERE user_id = ? ORDER BY name ASC",
            (user_id,),
        ).fetchall()
    return [
        SavedPrompt(id=r["id"], user_id=r["user_id"], name=r["name"],
                    content=r["content"], created_at=r["created_at"])
        for r in rows
    ]


async def list_saved_prompts(path: str | Path, user_id: int) -> list[SavedPrompt]:
    return await asyncio.to_thread(_list_saved_prompts_sync, Path(path), user_id)


def _delete_saved_prompt_sync(path: Path, user_id: int, name: str) -> bool:
    with _connect(path) as conn:
        cur = conn.execute(
            "DELETE FROM saved_prompts WHERE user_id = ? AND name = ?",
            (user_id, name),
        )
        return cur.rowcount > 0


async def delete_saved_prompt(path: str | Path, user_id: int, name: str) -> bool:
    return await asyncio.to_thread(_delete_saved_prompt_sync, Path(path), user_id, name)


# --- GDPR forgetme (#11) --------------------------------------------------

def _forget_user_sync(path: Path, user_id: int) -> None:
    """Delete ALL data for a user: sessions, messages, prefs, usage, saved prompts."""
    with _connect(path) as conn:
        # Get session IDs first for cascade
        rows = conn.execute(
            "SELECT id FROM sessions WHERE user_id = ?", (user_id,)
        ).fetchall()
        for row in rows:
            conn.execute("DELETE FROM messages WHERE session_id = ?", (row["id"],))
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM user_preferences WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM usage_log WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM saved_prompts WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM message_feedback WHERE user_id = ?", (user_id,))


async def forget_user(path: str | Path, user_id: int) -> None:
    """GDPR: permanently delete all user data."""
    await asyncio.to_thread(_forget_user_sync, Path(path), user_id)


# --- Audit Log (#28) -------------------------------------------------------

def _log_audit_sync(
    path: Path,
    admin_id: int,
    action: str,
    target_id: int | None,
    detail: str,
) -> None:
    with _connect(path) as conn:
        conn.execute(
            "INSERT INTO audit_log (admin_id, action, target_id, detail, ts) "
            "VALUES (?, ?, ?, ?, ?)",
            (admin_id, action, target_id, detail, _now()),
        )


async def log_audit(
    path: str | Path,
    admin_id: int,
    action: str,
    target_id: int | None = None,
    detail: str = "",
) -> None:
    await asyncio.to_thread(
        _log_audit_sync, Path(path), admin_id, action, target_id, detail
    )


def _get_audit_log_sync(path: Path, limit: int) -> list[AuditEntry]:
    with _connect(path) as conn:
        rows = conn.execute(
            "SELECT id, admin_id, action, target_id, detail, ts "
            "FROM audit_log ORDER BY ts DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [
        AuditEntry(
            id=r["id"], admin_id=r["admin_id"], action=r["action"],
            target_id=r["target_id"], detail=r["detail"], ts=r["ts"]
        )
        for r in rows
    ]


async def get_audit_log(path: str | Path, limit: int = 50) -> list[AuditEntry]:
    return await asyncio.to_thread(_get_audit_log_sync, Path(path), limit)


# --- Message Feedback (#7) ------------------------------------------------

def _save_feedback_sync(
    path: Path, user_id: int, session_id: int, message_id: int, feedback: str
) -> None:
    with _connect(path) as conn:
        conn.execute(
            "INSERT INTO message_feedback (user_id, session_id, message_id, feedback, ts) "
            "VALUES (?, ?, ?, ?, ?)",
            (user_id, session_id, message_id, feedback, _now()),
        )


async def save_feedback(
    path: str | Path, user_id: int, session_id: int, message_id: int, feedback: str
) -> None:
    await asyncio.to_thread(
        _save_feedback_sync, Path(path), user_id, session_id, message_id, feedback
    )


# --- Analytics / Insights (#18) -------------------------------------------

def _user_analytics_sync(path: Path, user_id: int) -> dict:
    """Return per-user analytics summary."""
    with _connect(path) as conn:
        sessions = conn.execute(
            "SELECT COUNT(*) AS c FROM sessions WHERE user_id = ?", (user_id,)
        ).fetchone()
        messages = conn.execute(
            "SELECT COUNT(*) AS c FROM messages m "
            "JOIN sessions s ON s.id = m.session_id WHERE s.user_id = ?",
            (user_id,),
        ).fetchone()
        tokens_row = conn.execute(
            "SELECT COALESCE(SUM(tokens_in),0) AS ti, COALESCE(SUM(tokens_out),0) AS to2 "
            "FROM usage_log WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        # Most-used models
        model_rows = conn.execute(
            "SELECT model, COUNT(*) AS c FROM usage_log WHERE user_id = ? "
            "GROUP BY model ORDER BY c DESC LIMIT 5",
            (user_id,),
        ).fetchall()
        # Activity by day (last 7 days)
        day_rows = conn.execute(
            "SELECT substr(ts,1,10) AS day, COUNT(*) AS c "
            "FROM usage_log WHERE user_id = ? "
            "GROUP BY day ORDER BY day DESC LIMIT 7",
            (user_id,),
        ).fetchall()

    return {
        "sessions": int(sessions["c"]) if sessions else 0,
        "messages": int(messages["c"]) if messages else 0,
        "tokens_in": int(tokens_row["ti"]) if tokens_row else 0,
        "tokens_out": int(tokens_row["to2"]) if tokens_row else 0,
        "top_models": [(r["model"], int(r["c"])) for r in model_rows],
        "daily_activity": [(r["day"], int(r["c"])) for r in day_rows],
    }


async def user_analytics(path: str | Path, user_id: int) -> dict:
    return await asyncio.to_thread(_user_analytics_sync, Path(path), user_id)


# --- get_all_messages (for share.py #6) ------------------------------------

def _get_all_messages_sync(path: Path, session_id: int) -> list[Message]:
    with _connect(path) as conn:
        rows = conn.execute(
            "SELECT id, role, content FROM messages WHERE session_id = ? ORDER BY id ASC",
            (session_id,),
        ).fetchall()
    return [Message(role=r["role"], content=r["content"], id=r["id"]) for r in rows]


async def get_all_messages(path: str | Path, session_id: int) -> list[Message]:
    """Return all messages in a session ordered by id (for HTML export)."""
    return await asyncio.to_thread(_get_all_messages_sync, Path(path), session_id)


# --- Reminders (#40) -------------------------------------------------------

from dataclasses import dataclass as _dc  # noqa: E402


@_dc(frozen=True)
class Reminder:
    id: int
    user_id: int
    chat_id: int
    text: str
    remind_at: str
    sent: bool
    created_at: str


def _save_reminder_sync(
    path: Path, user_id: int, chat_id: int, text: str, remind_at: str
) -> int:
    with _connect(path) as conn:
        cur = conn.execute(
            "INSERT INTO reminders (user_id, chat_id, text, remind_at, sent, created_at) "
            "VALUES (?, ?, ?, ?, 0, ?)",
            (user_id, chat_id, text, remind_at, _now()),
        )
        return int(cur.lastrowid or 0)


async def save_reminder(
    path: str | Path, user_id: int, chat_id: int, text: str, remind_at: str
) -> int:
    return await asyncio.to_thread(
        _save_reminder_sync, Path(path), user_id, chat_id, text, remind_at
    )


def _get_pending_reminders_sync(path: Path, before: str) -> list[Reminder]:
    with _connect(path) as conn:
        rows = conn.execute(
            "SELECT id, user_id, chat_id, text, remind_at, sent, created_at "
            "FROM reminders WHERE sent = 0 AND remind_at <= ? ORDER BY remind_at ASC",
            (before,),
        ).fetchall()
    return [
        Reminder(
            id=r["id"], user_id=r["user_id"], chat_id=r["chat_id"],
            text=r["text"], remind_at=r["remind_at"],
            sent=bool(r["sent"]), created_at=r["created_at"],
        )
        for r in rows
    ]


async def get_pending_reminders(path: str | Path, before: str) -> list[Reminder]:
    return await asyncio.to_thread(_get_pending_reminders_sync, Path(path), before)


def _mark_reminder_sent_sync(path: Path, reminder_id: int) -> None:
    with _connect(path) as conn:
        conn.execute("UPDATE reminders SET sent = 1 WHERE id = ?", (reminder_id,))


async def mark_reminder_sent(path: str | Path, reminder_id: int) -> None:
    await asyncio.to_thread(_mark_reminder_sent_sync, Path(path), reminder_id)


# --- Admin dashboard helpers (#17) ----------------------------------------

def _global_stats_sync(path: Path) -> dict:
    with _connect(path) as conn:
        users = conn.execute("SELECT COUNT(*) AS c FROM user_preferences").fetchone()
        sessions = conn.execute("SELECT COUNT(*) AS c FROM sessions").fetchone()
        messages = conn.execute("SELECT COUNT(*) AS c FROM messages").fetchone()
        tokens_row = conn.execute(
            "SELECT COALESCE(SUM(tokens_in),0) AS ti, COALESCE(SUM(tokens_out),0) AS to2 FROM usage_log"
        ).fetchone()
    return {
        "users": int(users["c"]) if users else 0,
        "sessions": int(sessions["c"]) if sessions else 0,
        "messages": int(messages["c"]) if messages else 0,
        "tokens_in": int(tokens_row["ti"]) if tokens_row else 0,
        "tokens_out": int(tokens_row["to2"]) if tokens_row else 0,
    }


async def global_stats(path: str | Path) -> dict:
    """Return global bot statistics for admin dashboard."""
    return await asyncio.to_thread(_global_stats_sync, Path(path))


def _list_all_users_sync(path: Path, limit: int) -> list[dict]:
    with _connect(path) as conn:
        rows = conn.execute(
            "SELECT user_id, default_model, persona, ui_language, onboarded, "
            "status, display_name, tts_enabled, privacy_mode, tier, created_at, updated_at "
            "FROM user_preferences ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [
        {
            "user_id": int(r["user_id"]),
            "default_model": r["default_model"],
            "persona": r["persona"],
            "ui_language": r["ui_language"],
            "onboarded": bool(r["onboarded"]),
            "status": r["status"],
            "display_name": r["display_name"] or "",
            "tts_enabled": bool(r["tts_enabled"]),
            "privacy_mode": r["privacy_mode"],
            "tier": r["tier"] if "tier" in r.keys() else "free",
            "created_at": r["created_at"],
        }
        for r in rows
    ]


async def list_all_users(path: str | Path, limit: int = 500) -> list[dict]:
    """Return all users as list of dicts (for admin dashboard)."""
    return await asyncio.to_thread(_list_all_users_sync, Path(path), limit)


def _list_recent_sessions_sync(path: Path, limit: int) -> list[Session]:
    with _connect(path) as conn:
        rows = conn.execute(
            "SELECT s.id, s.user_id, s.title, s.model, s.created_at, s.updated_at, "
            "s.persona, s.pinned, s.archived, s.summary, s.summary_until_id, "
            "COUNT(m.id) AS message_count "
            "FROM sessions s LEFT JOIN messages m ON m.session_id = s.id "
            "GROUP BY s.id ORDER BY s.updated_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [
        Session(
            id=r["id"], user_id=r["user_id"], title=r["title"], model=r["model"],
            created_at=r["created_at"], updated_at=r["updated_at"],
            message_count=int(r["message_count"]), persona=r["persona"] or "",
            pinned=bool(r["pinned"]), archived=bool(r["archived"]),
            summary=r["summary"] or "", summary_until_id=int(r["summary_until_id"]),
        )
        for r in rows
    ]


async def list_recent_sessions(path: str | Path, limit: int = 100) -> list[Session]:
    """Return recent sessions for admin dashboard."""
    return await asyncio.to_thread(_list_recent_sessions_sync, Path(path), limit)
