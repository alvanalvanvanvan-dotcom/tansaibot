"""Telegram bot that proxies messages to a Tans AI API Gateway."""
from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
from pathlib import Path

from dotenv import load_dotenv
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import db
import personas
import quick_prompts
import rate_limiter
import streaming
import ui
from markdown_utils import chunk_for_telegram, to_telegram_html
from tans_client import TansAIClient, TansAIError

logger = logging.getLogger(__name__)


# Pending-action keys stored in ``context.user_data``. They capture state
# carried between two messages (e.g. user pressed Quick Prompt -> we wait
# for their next message to be the input for that template).
PENDING_QUICK = "pending_quick"
PENDING_RENAME = "pending_rename"
PENDING_CUSTOM_PROMPT = "pending_custom_prompt"
PENDING_SEARCH = "pending_search"

# Per-user-per-chat cache of the most recently rendered AI placeholder
# message id, so the Regenerate button can edit the same message instead
# of spamming new ones.
LAST_AI_MESSAGE = "last_ai_message_id"


# --- Config -----------------------------------------------------------------

def _config() -> dict[str, str | int | float]:
    load_dotenv()
    required = ["TELEGRAM_BOT_TOKEN", "TANS_AI_API_KEY"]
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        raise SystemExit(
            "Environment variable wajib belum di-set: " + ", ".join(missing)
        )
    default_db_path = str(Path(__file__).resolve().parent / "chat_history.db")
    admin_ids: list[int] = []
    raw_admins = os.getenv("ADMIN_TELEGRAM_IDS", "").strip()
    if raw_admins:
        for chunk in raw_admins.replace(";", ",").split(","):
            chunk = chunk.strip()
            if chunk.lstrip("-").isdigit():
                admin_ids.append(int(chunk))
    return {
        "telegram_token": os.environ["TELEGRAM_BOT_TOKEN"],
        "tans_base_url": os.getenv("TANS_AI_BASE_URL", "http://localhost:20130/api/v1"),
        "tans_api_key": os.environ["TANS_AI_API_KEY"],
        "default_model": os.getenv("TANS_AI_DEFAULT_MODEL", "gpt-4o"),
        "history_max": int(os.getenv("HISTORY_MAX_MESSAGES", "20")),
        "timeout": float(os.getenv("TANS_AI_TIMEOUT", "60")),
        "db_path": os.getenv("CHAT_DB_PATH", default_db_path),
        "admin_ids": admin_ids,
        "auto_title": os.getenv("AUTO_TITLE", "1") not in ("0", "false", "False"),
        "rate_limit_per_minute": int(os.getenv("RATE_LIMIT_PER_MINUTE", "30")),
        "rate_limit_per_day": int(os.getenv("RATE_LIMIT_PER_DAY", "500")),
        "waitlist_mode": os.getenv("WAITLIST_MODE", "0") in ("1", "true", "True"),
    }


# --- Pref helpers -----------------------------------------------------------

async def _ensure_prefs(
    db_path: str,
    user_id: int,
    default_model: str,
    *,
    waitlist_mode: bool = False,
    admin_ids: tuple[int, ...] = (),
    display_name: str = "",
) -> db.UserPrefs:
    prefs = await db.get_user_prefs(db_path, user_id)
    if prefs is None:
        is_admin = user_id in admin_ids
        initial_status = (
            db.STATUS_ACTIVE
            if (is_admin or not waitlist_mode)
            else db.STATUS_WAITLIST
        )
        await db.upsert_user_prefs(
            db_path,
            user_id,
            default_model=default_model,
            persona="default",
            ui_language="id",
            onboarded=False,
            status=initial_status,
            display_name=display_name,
        )
        prefs = await db.get_user_prefs(db_path, user_id)
        assert prefs is not None
    elif display_name and prefs.display_name != display_name:
        # Keep the latest display name fresh so admin lists are useful.
        await db.upsert_user_prefs(db_path, user_id, display_name=display_name)
        prefs = await db.get_user_prefs(db_path, user_id) or prefs
    return prefs


def _active_model(prefs: db.UserPrefs, default: str) -> str:
    return prefs.default_model or default


def _t(prefs: db.UserPrefs, id_text: str, en_text: str) -> str:
    """Tiny translation helper: pick text based on user's UI language."""
    return en_text if prefs.ui_language == "en" else id_text


def _admin_ids(context: ContextTypes.DEFAULT_TYPE) -> tuple[int, ...]:
    return tuple(context.application.bot_data.get("admin_ids") or ())


def _is_admin(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    return user_id in _admin_ids(context)


def _waitlist_mode(context: ContextTypes.DEFAULT_TYPE) -> bool:
    return bool(context.application.bot_data.get("waitlist_mode"))


def _display_name_from_update(update: Update) -> str:
    user = update.effective_user
    if user is None:
        return ""
    parts: list[str] = []
    if user.username:
        parts.append("@" + user.username)
    full = " ".join(p for p in (user.first_name, user.last_name) if p)
    if full and full not in parts:
        parts.append(full)
    return " — ".join(parts)[:80]


async def _admit(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> tuple[db.UserPrefs | None, str | None]:
    """Ensure prefs + enforce waitlist & rate limit for chat actions.

    Returns ``(prefs, deny_text)``. When ``deny_text`` is non-empty the caller
    must show it to the user and bail (the user is either banned, on the
    waitlist, or being rate-limited).  When ``prefs`` is ``None`` we could not
    determine the user (caller should also bail).
    """
    if update.effective_user is None:
        return None, None
    user_id = update.effective_user.id
    db_path: str = context.application.bot_data["db_path"]
    default_model: str = context.application.bot_data["default_model"]
    admin_ids = _admin_ids(context)
    waitlist_mode = _waitlist_mode(context)
    prefs = await _ensure_prefs(
        db_path,
        user_id,
        default_model,
        waitlist_mode=waitlist_mode,
        admin_ids=admin_ids,
        display_name=_display_name_from_update(update),
    )
    # Admins always pass.
    if user_id in admin_ids:
        return prefs, None
    if prefs.status == db.STATUS_BANNED:
        return prefs, _t(
            prefs,
            "🚫 Akses kamu diblokir admin bot ini.",
            "🚫 Your access has been blocked by an admin.",
        )
    if prefs.status == db.STATUS_WAITLIST:
        return prefs, _t(
            prefs,
            "⏳ Kamu sedang di waitlist. Admin akan meninjau akses kamu.\n"
            f"Kalau perlu, kirim user ID ini ke admin: <code>{user_id}</code>",
            "⏳ You're on the waitlist. An admin must approve your access.\n"
            f"Send this user ID to the admin if asked: <code>{user_id}</code>",
        )
    rl: rate_limiter.RateLimiter | None = context.application.bot_data.get(
        "rate_limiter"
    )
    if rl is not None and not rl.is_disabled():
        result = await rl.check_and_consume(user_id)
        if not result.allowed:
            human = rate_limiter.format_retry(result.retry_after, prefs.ui_language)
            if result.reason == "day":
                return prefs, _t(
                    prefs,
                    f"⏱ Limit harian tercapai. Coba lagi nanti (sekitar {human}).",
                    f"⏱ Daily limit reached. Try again later (~{human}).",
                )
            return prefs, _t(
                prefs,
                f"⏱ Pelan-pelan, ya. Tunggu {human} lagi sebelum kirim lagi.",
                f"⏱ Slow down. Wait {human} before sending again.",
            )
    return prefs, None


# --- Session helpers --------------------------------------------------------

MAX_TITLE_LEN = 40


def _make_title(first_message: str) -> str:
    title = " ".join(first_message.split())
    if len(title) > MAX_TITLE_LEN:
        title = title[: MAX_TITLE_LEN - 1].rstrip() + "\u2026"
    return title or "(tanpa judul)"


async def _get_or_create_active_session(
    db_path: str, user_id: int, model: str, persona_key: str, context: ContextTypes.DEFAULT_TYPE
) -> int:
    sid = context.user_data.get("active_session_id") if context.user_data is not None else None
    if isinstance(sid, int):
        session = await db.get_session(db_path, sid)
        if session is not None and session.user_id == user_id:
            return sid
    new_sid = await db.create_session(db_path, user_id=user_id, model=model, persona=persona_key)
    if context.user_data is not None:
        context.user_data["active_session_id"] = new_sid
    return new_sid


def _build_prompt(
    persona_prompt: str, history: list[db.Message], new_message: str
) -> str:
    """Compose a single-string prompt the Tans AI /chat endpoint understands."""
    lines: list[str] = []
    if persona_prompt:
        lines.append(f"System: {persona_prompt}")
    for msg in history:
        prefix = "User" if msg.role == "user" else "Assistant"
        lines.append(f"{prefix}: {msg.content}")
    lines.append(f"User: {new_message}")
    lines.append("Assistant:")
    return "\n".join(lines) if lines else new_message


def _estimate_tokens(text: str) -> int:
    """Cheap local approximation: ~4 chars per token, min 1."""
    if not text:
        return 0
    return max(1, len(text) // 4)


# --- /start, /help, /status -------------------------------------------------

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    assert update.effective_user is not None
    assert update.message is not None
    db_path: str = context.application.bot_data["db_path"]
    default_model: str = context.application.bot_data["default_model"]
    prefs = await _ensure_prefs(db_path, update.effective_user.id, default_model)

    if not prefs.onboarded:
        await _start_onboarding(update, context, prefs)
        return

    persona = personas.get(prefs.persona)
    model = _active_model(prefs, default_model)
    msg = _t(
        prefs,
        (
            f"Halo! Saya bot AI yang terhubung ke Tans AI.\n\n"
            f"Model aktif: <code>{ui.escape(model)}</code>\n"
            f"Persona: {persona.emoji} <b>{persona.label}</b>\n\n"
            "Tap salah satu tombol di bawah, atau ketik pesan apa saja untuk chat."
        ),
        (
            f"Hi! I'm an AI bot connected to Tans AI.\n\n"
            f"Active model: <code>{ui.escape(model)}</code>\n"
            f"Persona: {persona.emoji} <b>{persona.label}</b>\n\n"
            "Tap a button below or just type a message to chat."
        ),
    )
    await update.message.reply_text(
        msg, parse_mode=ParseMode.HTML, reply_markup=ui.main_reply_keyboard()
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    assert update.message is not None
    assert update.effective_user is not None
    db_path: str = context.application.bot_data["db_path"]
    default_model: str = context.application.bot_data["default_model"]
    prefs = await _ensure_prefs(db_path, update.effective_user.id, default_model)
    text = _t(
        prefs,
        (
            "<b>Cara pakai bot:</b>\n"
            "Gunakan tombol di bawah area ketik, atau ketik command:\n\n"
            f"\u2022 <b>{ui.BTN_NEW_CHAT}</b> / /new — mulai percakapan baru\n"
            f"\u2022 <b>{ui.BTN_HISTORY}</b> / /history — riwayat percakapan\n"
            f"\u2022 <b>{ui.BTN_QUICK}</b> / /quick — template prompt cepat\n"
            f"\u2022 <b>{ui.BTN_PERSONA}</b> / /persona — ganti gaya AI\n"
            f"\u2022 <b>{ui.BTN_MODELS}</b> / /models — pilih model\n"
            f"\u2022 <b>{ui.BTN_STATUS}</b> / /status — cek koneksi\n"
            f"\u2022 <b>{ui.BTN_SETTINGS}</b> / /settings — pengaturan\n"
            f"\u2022 <b>{ui.BTN_RESET}</b> / /reset — hapus sesi aktif\n\n"
            "/find &lt;kata&gt; — cari di riwayat\n"
            "/export — export sesi aktif sebagai file\n"
            "/stats — statistik penggunaan kamu\n"
            "/model &lt;nama&gt; — ganti model lewat teks\n\n"
            "Untuk chat, langsung ketik pesan apa saja. Bot mengingat seluruh "
            "percakapan di sesi aktif (tersimpan permanen ke SQLite)."
        ),
        (
            "<b>How to use:</b>\n"
            "Tap the buttons below or use these commands:\n\n"
            f"\u2022 <b>{ui.BTN_NEW_CHAT}</b> / /new — start a new chat\n"
            f"\u2022 <b>{ui.BTN_HISTORY}</b> / /history — past conversations\n"
            f"\u2022 <b>{ui.BTN_QUICK}</b> / /quick — quick-prompt templates\n"
            f"\u2022 <b>{ui.BTN_PERSONA}</b> / /persona — change AI style\n"
            f"\u2022 <b>{ui.BTN_MODELS}</b> / /models — pick a model\n"
            f"\u2022 <b>{ui.BTN_STATUS}</b> / /status — check connection\n"
            f"\u2022 <b>{ui.BTN_SETTINGS}</b> / /settings — preferences\n"
            f"\u2022 <b>{ui.BTN_RESET}</b> / /reset — delete the active chat\n\n"
            "/find &lt;word&gt; — search history\n"
            "/export — export the active session\n"
            "/stats — your usage stats\n"
            "/model &lt;name&gt; — switch model by text\n\n"
            "To chat, just type any message. The bot remembers the whole "
            "active session (persisted to SQLite)."
        ),
    )
    await update.message.reply_text(
        text, parse_mode=ParseMode.HTML, reply_markup=ui.main_reply_keyboard()
    )


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    assert update.message is not None
    client: TansAIClient = context.application.bot_data["tans_client"]
    try:
        info = await client.status()
    except TansAIError as exc:
        await update.message.reply_text(
            f"<b>Status: GAGAL</b>\n<code>{ui.escape(str(exc))}</code>",
            parse_mode=ParseMode.HTML,
        )
        return
    pretty = json.dumps(info, indent=2, ensure_ascii=False)
    if len(pretty) > 3500:
        pretty = pretty[:3500] + "\n... (truncated)"
    await update.message.reply_text(
        f"<b>Status: OK</b>\n<pre>{ui.escape(pretty)}</pre>",
        parse_mode=ParseMode.HTML,
    )


# --- /models ----------------------------------------------------------------

async def models_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    assert update.message is not None
    assert update.effective_user is not None
    db_path: str = context.application.bot_data["db_path"]
    default_model: str = context.application.bot_data["default_model"]
    prefs = await _ensure_prefs(db_path, update.effective_user.id, default_model)
    client: TansAIClient = context.application.bot_data["tans_client"]
    try:
        models = await client.list_models()
    except TansAIError as exc:
        await update.message.reply_text(f"Gagal mengambil daftar model: {exc}")
        return
    if not models:
        await update.message.reply_text("Tidak ada model yang dikembalikan oleh API.")
        return

    active = _active_model(prefs, default_model)
    keyboard = ui.models_keyboard(models, active)
    if not keyboard.inline_keyboard:
        formatted = "\n".join(f"- <code>{ui.escape(m)}</code>" for m in models)
        await update.message.reply_text(
            f"<b>Model tersedia:</b>\n{formatted}\n\n"
            "Pakai <code>/model &lt;nama&gt;</code> untuk mengganti.",
            parse_mode=ParseMode.HTML,
        )
        return

    await update.message.reply_text(
        _t(
            prefs,
            f"<b>Pilih model AI</b> (aktif: <code>{ui.escape(active)}</code>):\n"
            "Tap salah satu di bawah, atau ketik <code>/model &lt;nama&gt;</code>.",
            f"<b>Choose an AI model</b> (active: <code>{ui.escape(active)}</code>):\n"
            "Tap one below or use <code>/model &lt;name&gt;</code>.",
        ),
        reply_markup=keyboard,
        parse_mode=ParseMode.HTML,
    )


async def model_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or query.data is None or update.effective_user is None:
        return
    if not query.data.startswith(ui.MODEL_PREFIX):
        await query.answer()
        return
    new_model = query.data[len(ui.MODEL_PREFIX):]
    if not new_model:
        await query.answer("Model tidak valid.", show_alert=True)
        return
    db_path: str = context.application.bot_data["db_path"]
    default_model: str = context.application.bot_data["default_model"]
    user_id = update.effective_user.id
    await _ensure_prefs(db_path, user_id, default_model)
    await db.upsert_user_prefs(db_path, user_id, default_model=new_model)
    await query.answer(f"Model diganti ke {new_model}")

    client: TansAIClient = context.application.bot_data["tans_client"]
    try:
        models = await client.list_models()
    except TansAIError:
        models = []
    if models and query.message is not None:
        keyboard = ui.models_keyboard(models, new_model)
        try:
            await query.edit_message_text(
                f"<b>Pilih model AI</b> (aktif: <code>{ui.escape(new_model)}</code>):\n"
                "Tap salah satu di bawah, atau ketik <code>/model &lt;nama&gt;</code>.",
                reply_markup=keyboard,
                parse_mode=ParseMode.HTML,
            )
            return
        except BadRequest:
            logger.debug("Failed to edit /models message", exc_info=True)
    if query.message is not None:
        await query.message.reply_text(
            f"Model diganti ke <code>{ui.escape(new_model)}</code>.",
            parse_mode=ParseMode.HTML,
        )


async def model_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    assert update.message is not None
    assert update.effective_user is not None
    db_path: str = context.application.bot_data["db_path"]
    default_model: str = context.application.bot_data["default_model"]
    prefs = await _ensure_prefs(db_path, update.effective_user.id, default_model)
    args = context.args or []
    if not args:
        current = _active_model(prefs, default_model)
        await update.message.reply_text(
            f"Model aktif kamu: <code>{ui.escape(current)}</code>\n"
            "Pakai <code>/model &lt;nama&gt;</code> untuk mengganti, atau /models untuk daftar.",
            parse_mode=ParseMode.HTML,
        )
        return
    new_model = args[0].strip()
    if not new_model:
        await update.message.reply_text("Nama model tidak boleh kosong.")
        return
    await db.upsert_user_prefs(db_path, update.effective_user.id, default_model=new_model)
    await update.message.reply_text(
        f"Model diganti ke <code>{ui.escape(new_model)}</code>.",
        parse_mode=ParseMode.HTML,
    )


# --- /persona ---------------------------------------------------------------

async def persona_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    assert update.message is not None
    assert update.effective_user is not None
    db_path: str = context.application.bot_data["db_path"]
    default_model: str = context.application.bot_data["default_model"]
    prefs = await _ensure_prefs(db_path, update.effective_user.id, default_model)
    persona = personas.get(prefs.persona)
    lines = [
        f"<b>Pilih persona AI</b>",
        f"Aktif: {persona.emoji} <b>{ui.escape(persona.label)}</b>",
        f"<i>{ui.escape(persona.description)}</i>",
        "",
        "Tap salah satu di bawah untuk ganti.",
    ]
    await update.message.reply_text(
        "\n".join(lines),
        reply_markup=ui.personas_keyboard(prefs.persona),
        parse_mode=ParseMode.HTML,
    )


async def persona_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or query.data is None or update.effective_user is None:
        return
    if not query.data.startswith(ui.PERSONA_PREFIX):
        await query.answer()
        return
    key = query.data[len(ui.PERSONA_PREFIX):]
    db_path: str = context.application.bot_data["db_path"]
    default_model: str = context.application.bot_data["default_model"]
    user_id = update.effective_user.id
    prefs = await _ensure_prefs(db_path, user_id, default_model)

    if key == "custom":
        if context.user_data is not None:
            context.user_data[PENDING_CUSTOM_PROMPT] = True
        await query.answer()
        if query.message is not None:
            current = prefs.custom_system_prompt or "(belum ada)"
            await query.message.reply_text(
                "Kirim teks system prompt buatanmu (akan dipakai untuk semua "
                "percakapan baru). Saat ini:\n\n"
                f"<code>{ui.escape(current)}</code>",
                parse_mode=ParseMode.HTML,
            )
        return

    if key not in personas.PERSONAS:
        await query.answer("Persona tidak dikenal.", show_alert=True)
        return
    await db.upsert_user_prefs(db_path, user_id, persona=key)
    new_persona = personas.get(key)
    await query.answer(f"Persona: {new_persona.label}")
    if query.message is not None:
        try:
            await query.edit_message_text(
                f"<b>Persona</b>: {new_persona.emoji} <b>{ui.escape(new_persona.label)}</b>\n"
                f"<i>{ui.escape(new_persona.description)}</i>",
                reply_markup=ui.personas_keyboard(key),
                parse_mode=ParseMode.HTML,
            )
        except BadRequest:
            pass


# --- /quick -----------------------------------------------------------------

async def quick_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    assert update.message is not None
    await update.message.reply_text(
        "<b>Quick Prompt</b>\nPilih template, lalu kirim isinya di pesan "
        "berikutnya. Bot akan menjalankan prompt yang sudah dioptimasi.",
        reply_markup=ui.quick_prompts_keyboard(),
        parse_mode=ParseMode.HTML,
    )


async def quick_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or query.data is None or update.effective_user is None:
        return
    if not query.data.startswith(ui.QUICK_PREFIX):
        await query.answer()
        return
    key = query.data[len(ui.QUICK_PREFIX):]
    qp = quick_prompts.get(key)
    if qp is None:
        await query.answer("Template tidak ditemukan.", show_alert=True)
        return
    if context.user_data is not None:
        context.user_data[PENDING_QUICK] = key
    await query.answer(f"Template: {qp.label}")
    if query.message is not None:
        await query.message.reply_text(
            f"{qp.emoji} <b>{ui.escape(qp.label)}</b>\n\n{ui.escape(qp.placeholder)}",
            parse_mode=ParseMode.HTML,
        )


# --- /new -------------------------------------------------------------------

async def new_chat_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    assert update.message is not None
    assert update.effective_user is not None
    user_id = update.effective_user.id
    db_path: str = context.application.bot_data["db_path"]
    default_model: str = context.application.bot_data["default_model"]
    prefs = await _ensure_prefs(db_path, user_id, default_model)
    model = _active_model(prefs, default_model)
    new_sid = await db.create_session(
        db_path, user_id=user_id, model=model, persona=prefs.persona
    )
    if context.user_data is not None:
        context.user_data["active_session_id"] = new_sid
        # Clear pending actions for this user.
        for key in (PENDING_QUICK, PENDING_RENAME, PENDING_CUSTOM_PROMPT, PENDING_SEARCH):
            context.user_data.pop(key, None)
    persona = personas.get(prefs.persona)
    await update.message.reply_text(
        "\U0001f195 <b>Percakapan baru dimulai</b>\n"
        f"Model: <code>{ui.escape(model)}</code>\n"
        f"Persona: {persona.emoji} <b>{ui.escape(persona.label)}</b>\n\n"
        "Kirim pesan apa saja untuk memulai.",
        parse_mode=ParseMode.HTML,
        reply_markup=ui.main_reply_keyboard(),
    )


# --- /history ---------------------------------------------------------------

HISTORY_PAGE_SIZE = 8


async def _render_history(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    page: int,
    edit_message: bool = False,
) -> None:
    assert update.effective_user is not None
    user_id = update.effective_user.id
    db_path: str = context.application.bot_data["db_path"]
    total = await db.count_sessions(db_path, user_id)
    if total == 0:
        msg = (
            "Belum ada riwayat percakapan. Tap <b>New Chat</b> atau langsung "
            "kirim pesan untuk memulai."
        )
        if update.message is not None and not edit_message:
            await update.message.reply_text(msg, parse_mode=ParseMode.HTML)
        elif update.callback_query is not None and update.callback_query.message is not None:
            await update.callback_query.message.reply_text(msg, parse_mode=ParseMode.HTML)
        return

    page = max(0, page)
    sessions = await db.list_sessions(
        db_path, user_id=user_id, limit=HISTORY_PAGE_SIZE, offset=page * HISTORY_PAGE_SIZE
    )
    info = ui.PageInfo(page=page, limit=HISTORY_PAGE_SIZE, total=total)
    active = (
        context.user_data.get("active_session_id") if context.user_data is not None else None
    )
    keyboard = ui.history_keyboard(
        sessions, active if isinstance(active, int) else None, info
    )
    text = (
        f"<b>Riwayat percakapan</b> \u2014 hal. {info.page + 1}/{info.pages} "
        f"(total {total} sesi)\n"
        "Tap nama sesi untuk lanjutkan, atau \u2026 untuk menu (rename, pin, hapus)."
    )
    if (
        edit_message
        and update.callback_query is not None
        and update.callback_query.message is not None
    ):
        try:
            await update.callback_query.message.edit_text(
                text, reply_markup=keyboard, parse_mode=ParseMode.HTML
            )
            return
        except BadRequest:
            pass
    target = (
        update.message
        if update.message is not None
        else (update.callback_query.message if update.callback_query is not None else None)
    )
    if target is not None:
        await target.reply_text(
            text, reply_markup=keyboard, parse_mode=ParseMode.HTML
        )


async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _render_history(update, context, page=0)


async def history_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or query.data is None:
        return
    if not query.data.startswith(ui.SESSION_PAGE_PREFIX):
        await query.answer()
        return
    rest = query.data[len(ui.SESSION_PAGE_PREFIX):]
    if rest == "noop":
        await query.answer()
        return
    try:
        page = int(rest)
    except ValueError:
        await query.answer()
        return
    await query.answer()
    await _render_history(update, context, page=page, edit_message=True)


async def load_session_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    if query is None or query.data is None or update.effective_user is None:
        return
    if not query.data.startswith(ui.SESSION_PREFIX):
        await query.answer()
        return
    try:
        sid = int(query.data[len(ui.SESSION_PREFIX):])
    except ValueError:
        await query.answer("Session ID tidak valid.", show_alert=True)
        return

    db_path: str = context.application.bot_data["db_path"]
    session = await db.get_session(db_path, sid)
    if session is None or session.user_id != update.effective_user.id:
        await query.answer("Sesi tidak ditemukan.", show_alert=True)
        return

    user_id = update.effective_user.id
    if context.user_data is not None:
        context.user_data["active_session_id"] = sid
    # Sync user prefs to the session's model & persona on resume.
    await db.upsert_user_prefs(
        db_path, user_id, default_model=session.model, persona=session.persona or "default"
    )
    await query.answer("Sesi dimuat.")

    last_messages = await db.get_messages(db_path, sid, limit=2)
    preview_lines: list[str] = []
    for m in last_messages:
        role = "Kamu" if m.role == "user" else "AI"
        content = m.content if len(m.content) <= 200 else m.content[:200] + "\u2026"
        preview_lines.append(f"<b>{role}:</b> {ui.escape(content)}")
    preview = "\n".join(preview_lines) if preview_lines else "(belum ada pesan di sesi ini)"

    title = session.title or "(belum ada pesan)"
    persona = personas.get(session.persona)
    text = (
        f"\u25b6\ufe0f <b>Melanjutkan:</b> {ui.escape(title)}\n"
        f"Model: <code>{ui.escape(session.model)}</code>  "
        f"Persona: {persona.emoji} {ui.escape(persona.label)}\n"
        f"Pesan: {session.message_count}\n\n"
        f"{preview}\n\n"
        "Ketik pesan untuk melanjutkan."
    )
    if query.message is not None:
        try:
            await query.edit_message_text(text, parse_mode=ParseMode.HTML)
            return
        except BadRequest:
            pass
        await query.message.reply_text(text, parse_mode=ParseMode.HTML)


# --- Session action menu (rename/pin/delete/archive/export) ----------------

async def session_action_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    if query is None or query.data is None or update.effective_user is None:
        return
    if not query.data.startswith(ui.SESSION_ACTION_PREFIX):
        await query.answer()
        return
    payload = query.data[len(ui.SESSION_ACTION_PREFIX):]
    try:
        action, sid_str = payload.split(":", 1)
        sid = int(sid_str)
    except (ValueError, IndexError):
        await query.answer("Format aksi tidak dikenal.", show_alert=True)
        return

    db_path: str = context.application.bot_data["db_path"]
    session = await db.get_session(db_path, sid)
    if session is None or session.user_id != update.effective_user.id:
        await query.answer("Sesi tidak ditemukan.", show_alert=True)
        return

    if action == "menu":
        await query.answer()
        if query.message is not None:
            try:
                await query.edit_message_reply_markup(
                    reply_markup=ui.session_action_menu(session)
                )
            except BadRequest:
                await query.message.reply_text(
                    f"Menu untuk: <b>{ui.escape(session.title or '(tanpa judul)')}</b>",
                    reply_markup=ui.session_action_menu(session),
                    parse_mode=ParseMode.HTML,
                )
        return
    if action == "back":
        await query.answer()
        await _render_history(update, context, page=0, edit_message=True)
        return
    if action == "pin":
        await db.set_pin(db_path, sid, not session.pinned)
        await query.answer("Dipin" if not session.pinned else "Unpin")
        await _render_history(update, context, page=0, edit_message=True)
        return
    if action == "archive":
        await db.set_archive(db_path, sid, not session.archived)
        await query.answer("Diarchive" if not session.archived else "Unarchive")
        await _render_history(update, context, page=0, edit_message=True)
        return
    if action == "rename":
        if context.user_data is not None:
            context.user_data[PENDING_RENAME] = sid
        await query.answer()
        if query.message is not None:
            await query.message.reply_text(
                "Kirim judul baru untuk sesi ini di pesan berikutnya. "
                "Kirim /cancel untuk batalkan."
            )
        return
    if action == "delete":
        await query.answer()
        if query.message is not None:
            try:
                await query.edit_message_text(
                    f"Hapus sesi <b>{ui.escape(session.title or '(tanpa judul)')}</b>?\n"
                    "Tindakan ini tidak bisa dibatalkan.",
                    reply_markup=ui.confirm_delete_keyboard(sid),
                    parse_mode=ParseMode.HTML,
                )
            except BadRequest:
                pass
        return
    if action == "delconfirm":
        await db.delete_session(db_path, sid)
        await query.answer("Sesi dihapus.")
        if context.user_data is not None and context.user_data.get("active_session_id") == sid:
            context.user_data.pop("active_session_id", None)
        await _render_history(update, context, page=0, edit_message=True)
        return

    await query.answer()


# --- /reset -----------------------------------------------------------------

async def reset_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    assert update.message is not None
    assert update.effective_user is not None
    db_path: str = context.application.bot_data["db_path"]
    sid = (
        context.user_data.pop("active_session_id", None) if context.user_data is not None else None
    )
    if sid is None:
        await update.message.reply_text(
            "Tidak ada percakapan aktif yang perlu dihapus.\n"
            "Tap <b>New Chat</b> untuk memulai.",
            parse_mode=ParseMode.HTML,
        )
        return
    await db.delete_session(db_path, sid)
    await update.message.reply_text(
        "Percakapan aktif sudah dihapus dari riwayat.\n"
        "Tap <b>New Chat</b> atau kirim pesan untuk memulai sesi baru.",
        parse_mode=ParseMode.HTML,
    )


# --- /settings --------------------------------------------------------------

async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    assert update.message is not None
    assert update.effective_user is not None
    db_path: str = context.application.bot_data["db_path"]
    default_model: str = context.application.bot_data["default_model"]
    prefs = await _ensure_prefs(db_path, update.effective_user.id, default_model)
    model = _active_model(prefs, default_model)
    persona = personas.get(prefs.persona)
    text = _t(
        prefs,
        (
            "<b>Pengaturan</b>\n\n"
            f"\u2022 Model: <code>{ui.escape(model)}</code>\n"
            f"\u2022 Persona: {persona.emoji} <b>{ui.escape(persona.label)}</b>\n"
            f"\u2022 Bahasa: {'Indonesia' if prefs.ui_language == 'id' else 'English'}\n"
        ),
        (
            "<b>Settings</b>\n\n"
            f"\u2022 Model: <code>{ui.escape(model)}</code>\n"
            f"\u2022 Persona: {persona.emoji} <b>{ui.escape(persona.label)}</b>\n"
            f"\u2022 Language: {'Indonesia' if prefs.ui_language == 'id' else 'English'}\n"
        ),
    )
    await update.message.reply_text(
        text,
        reply_markup=ui.settings_keyboard(prefs, model),
        parse_mode=ParseMode.HTML,
    )


async def settings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or query.data is None or update.effective_user is None:
        return
    if not query.data.startswith(ui.SETTINGS_PREFIX):
        await query.answer()
        return
    action = query.data[len(ui.SETTINGS_PREFIX):]
    db_path: str = context.application.bot_data["db_path"]
    default_model: str = context.application.bot_data["default_model"]
    user_id = update.effective_user.id
    prefs = await _ensure_prefs(db_path, user_id, default_model)
    if action == "model":
        await query.answer()
        await models_command_via_query(update, context)
        return
    if action == "persona":
        await query.answer()
        if query.message is not None:
            await query.message.reply_text(
                "Pilih persona:",
                reply_markup=ui.personas_keyboard(prefs.persona),
            )
        return
    if action == "lang":
        await query.answer()
        if query.message is not None:
            await query.message.reply_text(
                "Pilih bahasa antarmuka:",
                reply_markup=ui.language_keyboard(prefs.ui_language),
            )
        return
    if action == "stats":
        await query.answer()
        totals = await db.usage_totals(db_path, user_id)
        sessions_count = await db.count_sessions(
            db_path, user_id, include_archived=True
        )
        text = (
            "<b>Statistik kamu</b>\n"
            f"\u2022 Sesi: <b>{sessions_count}</b>\n"
            f"\u2022 Pesan diproses: <b>{totals.messages}</b>\n"
            f"\u2022 Estimasi token in / out: "
            f"<b>{totals.tokens_in}</b> / <b>{totals.tokens_out}</b>\n"
        )
        if query.message is not None:
            await query.message.reply_text(text, parse_mode=ParseMode.HTML)
        return
    if action == "custom_prompt":
        await query.answer()
        if context.user_data is not None:
            context.user_data[PENDING_CUSTOM_PROMPT] = True
        if query.message is not None:
            current = prefs.custom_system_prompt or "(belum ada)"
            await query.message.reply_text(
                "Kirim system prompt baru di pesan berikutnya.\n"
                f"Saat ini:\n<code>{ui.escape(current)}</code>",
                parse_mode=ParseMode.HTML,
            )
        return


async def models_command_via_query(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if update.callback_query is None or update.callback_query.message is None:
        return
    assert update.effective_user is not None
    db_path: str = context.application.bot_data["db_path"]
    default_model: str = context.application.bot_data["default_model"]
    prefs = await _ensure_prefs(db_path, update.effective_user.id, default_model)
    client: TansAIClient = context.application.bot_data["tans_client"]
    try:
        models = await client.list_models()
    except TansAIError as exc:
        await update.callback_query.message.reply_text(f"Gagal: {exc}")
        return
    if not models:
        await update.callback_query.message.reply_text("API tidak mengembalikan daftar model.")
        return
    active = _active_model(prefs, default_model)
    await update.callback_query.message.reply_text(
        f"<b>Pilih model AI</b> (aktif: <code>{ui.escape(active)}</code>)",
        reply_markup=ui.models_keyboard(models, active),
        parse_mode=ParseMode.HTML,
    )


async def language_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or query.data is None or update.effective_user is None:
        return
    if not query.data.startswith(ui.LANG_PREFIX):
        await query.answer()
        return
    lang = query.data[len(ui.LANG_PREFIX):]
    if lang not in ("id", "en"):
        await query.answer()
        return
    db_path: str = context.application.bot_data["db_path"]
    await db.upsert_user_prefs(db_path, update.effective_user.id, ui_language=lang)
    await query.answer("OK")
    if query.message is not None:
        try:
            await query.edit_message_reply_markup(
                reply_markup=ui.language_keyboard(lang)
            )
        except BadRequest:
            pass


# --- Onboarding -------------------------------------------------------------

async def _start_onboarding(
    update: Update, context: ContextTypes.DEFAULT_TYPE, prefs: db.UserPrefs
) -> None:
    msg = update.message or (
        update.callback_query.message if update.callback_query is not None else None
    )
    if msg is None:
        return
    text = (
        "\U0001f44b <b>Selamat datang!</b>\n\n"
        "Aku bot AI yang bisa kamu ajak ngobrol kapan saja. Sebelum mulai, "
        "yuk atur preferensi sebentar (3 langkah cepat).\n\n"
        "<b>Langkah 1/3 \u2014 Pilih bahasa:</b>"
    )
    await msg.reply_text(
        text, parse_mode=ParseMode.HTML, reply_markup=ui.onboarding_step1_keyboard()
    )


async def onboarding_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    if query is None or query.data is None or update.effective_user is None:
        return
    if not query.data.startswith(ui.ONBOARD_PREFIX):
        await query.answer()
        return
    payload = query.data[len(ui.ONBOARD_PREFIX):]
    db_path: str = context.application.bot_data["db_path"]
    default_model: str = context.application.bot_data["default_model"]
    user_id = update.effective_user.id

    if payload.startswith("lang:"):
        lang = payload.split(":", 1)[1]
        if lang not in ("id", "en"):
            await query.answer()
            return
        await db.upsert_user_prefs(db_path, user_id, ui_language=lang)
        await query.answer()
        if query.message is not None:
            try:
                await query.edit_message_text(
                    "<b>Langkah 2/3 \u2014 Pilih persona AI:</b>\n"
                    "Persona adalah \u201cgaya\u201d AI saat menjawab kamu.",
                    parse_mode=ParseMode.HTML,
                    reply_markup=ui.onboarding_persona_keyboard(),
                )
            except BadRequest:
                pass
        return
    if payload.startswith("persona:"):
        key = payload.split(":", 1)[1]
        if key not in personas.PERSONAS:
            await query.answer()
            return
        await db.upsert_user_prefs(db_path, user_id, persona=key)
        await query.answer()
        client: TansAIClient = context.application.bot_data["tans_client"]
        try:
            models = await client.list_models()
        except TansAIError:
            models = []
        prefs = await _ensure_prefs(db_path, user_id, default_model)
        active = _active_model(prefs, default_model)
        if not models:
            # Skip model step if we couldn't fetch the list.
            await db.upsert_user_prefs(db_path, user_id, onboarded=True)
            if query.message is not None:
                try:
                    await query.edit_message_text(
                        "Onboarding selesai \u2014 langsung ketik pesan untuk chat. "
                        "Kamu bisa ubah preferensi di /settings.",
                        parse_mode=ParseMode.HTML,
                    )
                except BadRequest:
                    pass
            return
        if query.message is not None:
            try:
                await query.edit_message_text(
                    "<b>Langkah 3/3 \u2014 Pilih default model:</b>\n"
                    f"(aktif: <code>{ui.escape(active)}</code>)",
                    parse_mode=ParseMode.HTML,
                    reply_markup=ui.models_keyboard(models, active),
                )
            except BadRequest:
                pass
        await db.upsert_user_prefs(db_path, user_id, onboarded=True)
        return
    await query.answer()


# --- Search -----------------------------------------------------------------

async def search_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    assert update.message is not None
    assert update.effective_user is not None
    db_path: str = context.application.bot_data["db_path"]
    args = context.args or []
    if not args:
        if context.user_data is not None:
            context.user_data[PENDING_SEARCH] = True
        await update.message.reply_text(
            "Ketik kata kunci yang ingin dicari di pesan berikutnya."
        )
        return
    query = " ".join(args).strip()
    await _do_search(update, context, query)


async def search_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or query.data is None:
        return
    if context.user_data is not None:
        context.user_data[PENDING_SEARCH] = True
    await query.answer()
    if query.message is not None:
        await query.message.reply_text(
            "Ketik kata kunci yang ingin dicari di pesan berikutnya."
        )


async def _do_search(
    update: Update, context: ContextTypes.DEFAULT_TYPE, query: str
) -> None:
    assert update.effective_user is not None
    db_path: str = context.application.bot_data["db_path"]
    user_id = update.effective_user.id
    sessions = await db.search_sessions(db_path, user_id, query, limit=15)
    target = update.message or (
        update.callback_query.message if update.callback_query is not None else None
    )
    if target is None:
        return
    if not sessions:
        await target.reply_text(f"Tidak ada hasil untuk: <i>{ui.escape(query)}</i>", parse_mode=ParseMode.HTML)
        return
    rows: list[list[InlineKeyboardButton]] = []
    for s in sessions:
        title = s.title or "(tanpa judul)"
        if len(title) > 50:
            title = title[:49] + "\u2026"
        rows.append(
            [
                InlineKeyboardButton(
                    text=title, callback_data=f"{ui.SESSION_PREFIX}{s.id}"
                )
            ]
        )
    await target.reply_text(
        f"<b>{len(sessions)} hasil</b> untuk: <i>{ui.escape(query)}</i>",
        reply_markup=InlineKeyboardMarkup(rows),
        parse_mode=ParseMode.HTML,
    )


# --- Export -----------------------------------------------------------------

async def export_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    assert update.message is not None
    assert update.effective_user is not None
    sid = (
        context.user_data.get("active_session_id") if context.user_data is not None else None
    )
    if not isinstance(sid, int):
        await update.message.reply_text(
            "Tidak ada sesi aktif untuk di-export. Buka sesi dari History dulu."
        )
        return
    await _export_session(update, context, sid)


async def export_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or query.data is None or update.effective_user is None:
        return
    if not query.data.startswith(ui.EXPORT_PREFIX):
        await query.answer()
        return
    try:
        sid = int(query.data[len(ui.EXPORT_PREFIX):])
    except ValueError:
        await query.answer()
        return
    await query.answer("Menyiapkan export...")
    await _export_session(update, context, sid)


async def _export_session(
    update: Update, context: ContextTypes.DEFAULT_TYPE, sid: int
) -> None:
    assert update.effective_user is not None
    db_path: str = context.application.bot_data["db_path"]
    session = await db.get_session(db_path, sid)
    if session is None or session.user_id != update.effective_user.id:
        target = update.message or (
            update.callback_query.message if update.callback_query is not None else None
        )
        if target is not None:
            await target.reply_text("Sesi tidak ditemukan.")
        return
    messages = await db.get_all_messages(db_path, sid)
    lines = [
        f"# {session.title or '(tanpa judul)'}",
        "",
        f"- Model: `{session.model}`",
        f"- Persona: `{session.persona or 'default'}`",
        f"- Created: {session.created_at}",
        f"- Updated: {session.updated_at}",
        f"- Messages: {len(messages)}",
        "",
        "---",
        "",
    ]
    for m in messages:
        role = "**You**" if m.role == "user" else "**AI**"
        lines.append(role)
        lines.append("")
        lines.append(m.content)
        lines.append("")
    body = "\n".join(lines).encode("utf-8")
    safe_title = re.sub(r"[^a-zA-Z0-9_-]+", "_", session.title or f"session-{sid}").strip("_")
    filename = f"{safe_title[:40] or f'session-{sid}'}.md"
    target = update.message or (
        update.callback_query.message if update.callback_query is not None else None
    )
    if target is not None:
        await target.reply_document(document=body, filename=filename)


# --- /stats -----------------------------------------------------------------

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    assert update.message is not None
    assert update.effective_user is not None
    db_path: str = context.application.bot_data["db_path"]
    user_id = update.effective_user.id
    totals = await db.usage_totals(db_path, user_id)
    sessions_count = await db.count_sessions(db_path, user_id, include_archived=True)
    await update.message.reply_text(
        "<b>Statistik kamu</b>\n"
        f"\u2022 Sesi: <b>{sessions_count}</b>\n"
        f"\u2022 Pesan diproses: <b>{totals.messages}</b>\n"
        f"\u2022 Estimasi token in / out: "
        f"<b>{totals.tokens_in}</b> / <b>{totals.tokens_out}</b>\n",
        parse_mode=ParseMode.HTML,
    )


# --- /admin -----------------------------------------------------------------

async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    assert update.message is not None
    assert update.effective_user is not None
    admin_ids: list[int] = context.application.bot_data.get("admin_ids", [])
    if update.effective_user.id not in admin_ids:
        await update.message.reply_text("\u274c Command admin only.")
        return
    db_path: str = context.application.bot_data["db_path"]
    stats = await db.global_stats(db_path)
    await update.message.reply_text(
        "<b>Admin stats</b>\n"
        f"\u2022 Users: <b>{stats['users']}</b>\n"
        f"\u2022 Sessions: <b>{stats['sessions']}</b>\n"
        f"\u2022 Messages: <b>{stats['messages']}</b>\n\n"
        "<i>Gunakan /broadcast &lt;pesan&gt; untuk kirim pengumuman ke semua user.</i>",
        parse_mode=ParseMode.HTML,
    )


async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    assert update.message is not None
    assert update.effective_user is not None
    admin_ids: list[int] = context.application.bot_data.get("admin_ids", [])
    if update.effective_user.id not in admin_ids:
        await update.message.reply_text("\u274c Command admin only.")
        return
    if not context.args:
        await update.message.reply_text("Pakai: /broadcast <pesan>")
        return
    text = " ".join(context.args)
    db_path: str = context.application.bot_data["db_path"]
    targets = await db.list_user_ids(db_path)
    sent = 0
    for uid in targets:
        try:
            await context.bot.send_message(uid, f"\U0001f4e2 {text}")
            sent += 1
        except Exception:  # noqa: BLE001 - broadcast best-effort
            logger.debug("broadcast to %s failed", uid, exc_info=True)
    await update.message.reply_text(f"Broadcast terkirim ke {sent}/{len(targets)} user.")


# --- Admin: access management ----------------------------------------------

def _parse_user_id_arg(args: list[str] | None) -> int | None:
    if not args:
        return None
    raw = args[0].strip().lstrip("@")
    if not raw.lstrip("-").isdigit():
        return None
    try:
        return int(raw)
    except ValueError:
        return None


async def _notify_user_status_change(
    context: ContextTypes.DEFAULT_TYPE, target_user_id: int, new_status: str
) -> None:
    """Best-effort DM to inform a user that their access status changed."""
    messages = {
        db.STATUS_ACTIVE: "\u2705 Akses kamu sudah disetujui. Selamat datang!",
        db.STATUS_WAITLIST: "\u23f3 Akses kamu dipindahkan ke waitlist oleh admin.",
        db.STATUS_BANNED: "\U0001f6ab Akses kamu diblokir oleh admin.",
    }
    msg = messages.get(new_status)
    if not msg:
        return
    try:
        await context.bot.send_message(target_user_id, msg)
    except Exception:  # noqa: BLE001
        logger.debug("status notification to %s failed", target_user_id, exc_info=True)


async def _set_user_status_admin(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    new_status: str,
    *,
    success_text: str,
) -> None:
    assert update.message is not None
    assert update.effective_user is not None
    if not _is_admin(context, update.effective_user.id):
        await update.message.reply_text("\u274c Command admin only.")
        return
    target_id = _parse_user_id_arg(list(context.args or []))
    if target_id is None:
        await update.message.reply_text(
            "Pakai: /approve|/deny|/ban|/unban <telegram_user_id>"
        )
        return
    db_path: str = context.application.bot_data["db_path"]
    prefs = await db.get_user_prefs(db_path, target_id)
    if prefs is None:
        # Pre-create record so admin can pre-approve before user joins.
        await db.upsert_user_prefs(
            db_path,
            target_id,
            default_model=context.application.bot_data["default_model"],
            persona="default",
            ui_language="id",
            onboarded=False,
            status=new_status,
        )
    else:
        await db.upsert_user_prefs(db_path, target_id, status=new_status)
    await update.message.reply_text(
        f"{success_text} (user {target_id}, status={new_status})."
    )
    await _notify_user_status_change(context, target_id, new_status)


async def approve_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _set_user_status_admin(
        update, context, db.STATUS_ACTIVE, success_text="\u2705 User diizinkan"
    )


async def deny_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _set_user_status_admin(
        update,
        context,
        db.STATUS_WAITLIST,
        success_text="\u23f3 User dikembalikan ke waitlist",
    )


async def ban_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _set_user_status_admin(
        update, context, db.STATUS_BANNED, success_text="\U0001f6ab User diblokir"
    )


async def unban_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _set_user_status_admin(
        update, context, db.STATUS_ACTIVE, success_text="\u2705 User di-unban"
    )


def _fmt_user_row(user_id: int, name: str, ts: str) -> str:
    safe_name = html.escape(name) if name else "(tanpa nama)"
    return f"\u2022 <code>{user_id}</code> \u2014 {safe_name} <i>(sejak {ts})</i>"


async def waitlist_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    assert update.message is not None
    assert update.effective_user is not None
    if not _is_admin(context, update.effective_user.id):
        await update.message.reply_text("\u274c Command admin only.")
        return
    db_path: str = context.application.bot_data["db_path"]
    rows = await db.list_users_by_status(db_path, db.STATUS_WAITLIST, limit=50)
    if not rows:
        await update.message.reply_text(
            "\U0001f389 Tidak ada user di waitlist.\n\n"
            "<i>Tip: /approve &lt;user_id&gt; untuk izinkan, /ban &lt;user_id&gt; untuk blokir.</i>",
            parse_mode=ParseMode.HTML,
        )
        return
    lines = ["<b>\u23f3 Waitlist</b>"]
    for uid, name, ts in rows:
        lines.append(_fmt_user_row(uid, name, ts))
    lines.append(
        "\n<i>/approve &lt;user_id&gt; \u2014 izinkan. /ban &lt;user_id&gt; \u2014 blokir.</i>"
    )
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def users_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    assert update.message is not None
    assert update.effective_user is not None
    if not _is_admin(context, update.effective_user.id):
        await update.message.reply_text("\u274c Command admin only.")
        return
    db_path: str = context.application.bot_data["db_path"]
    counts = await db.count_users_by_status(db_path)
    total = sum(counts.values()) or 0
    lines = [
        "<b>\U0001f465 Users</b>",
        f"\u2022 Active: <b>{counts.get(db.STATUS_ACTIVE, 0)}</b>",
        f"\u2022 Waitlist: <b>{counts.get(db.STATUS_WAITLIST, 0)}</b>",
        f"\u2022 Banned: <b>{counts.get(db.STATUS_BANNED, 0)}</b>",
        f"\u2022 Total: <b>{total}</b>",
    ]
    waitlist_mode = _waitlist_mode(context)
    lines.append(
        f"\n<i>WAITLIST_MODE: <b>{'on' if waitlist_mode else 'off'}</b>. "
        f"User baru otomatis masuk waitlist kalau on.</i>"
    )
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


# --- /cancel ----------------------------------------------------------------

async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    assert update.message is not None
    if context.user_data is not None:
        for key in (PENDING_QUICK, PENDING_RENAME, PENDING_CUSTOM_PROMPT, PENDING_SEARCH):
            context.user_data.pop(key, None)
    await update.message.reply_text(
        "OK, aksi pending dibatalkan.", reply_markup=ui.main_reply_keyboard()
    )


# --- Chat -------------------------------------------------------------------

async def _send_ai_reply(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    sid: int,
    user_message: str,
    persist_user: bool,
) -> None:
    """Common path: call Tans AI with the user's message, render, persist.

    When ``persist_user`` is False, the user's prior turn is already stored
    (used by Regenerate).
    """
    assert update.effective_user is not None
    user_id = update.effective_user.id
    db_path: str = context.application.bot_data["db_path"]
    client: TansAIClient = context.application.bot_data["tans_client"]
    default_model: str = context.application.bot_data["default_model"]
    history_max: int = context.application.bot_data["history_max"]
    auto_title: bool = context.application.bot_data["auto_title"]
    prefs = await _ensure_prefs(db_path, user_id, default_model)
    session = await db.get_session(db_path, sid)
    if session is None:
        return
    model = session.model or _active_model(prefs, default_model)
    persona_key = session.persona or prefs.persona or "default"
    persona_prompt = personas.resolve_system_prompt(
        persona_key, prefs.custom_system_prompt
    )

    chat_id = update.effective_chat.id if update.effective_chat else user_id
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

    placeholder_text = streaming.frames_for(prefs.ui_language)[0]
    target = update.message or (
        update.callback_query.message if update.callback_query is not None else None
    )
    if target is None:
        return
    placeholder = await target.reply_text(placeholder_text)
    stop_event = asyncio.Event()
    anim_task = asyncio.create_task(
        streaming.animate(placeholder, stop_event, prefs.ui_language)
    )

    async def _keep_typing() -> None:
        # Telegram's typing indicator expires after ~5s; refresh every 4s.
        while not stop_event.is_set():
            try:
                await context.bot.send_chat_action(
                    chat_id=chat_id, action=ChatAction.TYPING
                )
            except Exception:  # noqa: BLE001 - best effort
                logger.debug("send_chat_action failed", exc_info=True)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=4.0)
            except asyncio.TimeoutError:
                continue

    typing_task = asyncio.create_task(_keep_typing())

    try:
        history = await db.get_messages(db_path, sid, limit=history_max)
        # If we're regenerating, the popped user message is `user_message`
        # and the history we got back does NOT include it.
        prompt = _build_prompt(persona_prompt, history if persist_user else history, user_message)
        try:
            reply = await client.chat(message=prompt, model=model)
        except TansAIError as exc:
            stop_event.set()
            await anim_task
            await placeholder.edit_text(f"Gagal memanggil Tans AI: {exc}")
            return
        except Exception:  # noqa: BLE001
            stop_event.set()
            await anim_task
            logger.exception("Unexpected error while calling Tans AI")
            await placeholder.edit_text(
                "Terjadi kesalahan tak terduga saat memanggil Tans AI. "
                "Cek log bot untuk detail."
            )
            return
    finally:
        stop_event.set()
        for task in (anim_task, typing_task):
            try:
                await task
            except Exception:  # noqa: BLE001
                pass

    reply = (reply or "").strip() or "(AI mengembalikan response kosong.)"
    if persist_user:
        await db.add_message(db_path, sid, role="user", content=user_message)
    await db.add_message(db_path, sid, role="assistant", content=reply)
    await db.log_usage(
        db_path,
        user_id=user_id,
        session_id=sid,
        model=model,
        tokens_in=_estimate_tokens(user_message)
        + sum(_estimate_tokens(m.content) for m in history),
        tokens_out=_estimate_tokens(reply),
    )

    # Render reply: chunk + HTML.
    chunks = chunk_for_telegram(reply)
    rendered_first = to_telegram_html(chunks[0])
    try:
        await placeholder.edit_text(
            rendered_first,
            parse_mode=ParseMode.HTML,
            reply_markup=ui.reply_actions_keyboard(sid),
            disable_web_page_preview=True,
        )
    except BadRequest:
        # Fallback: plain text if HTML parser fails.
        await placeholder.edit_text(
            chunks[0],
            reply_markup=ui.reply_actions_keyboard(sid),
        )
    for extra in chunks[1:]:
        try:
            await target.reply_text(
                to_telegram_html(extra),
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
        except BadRequest:
            await target.reply_text(extra)

    if (
        auto_title
        and persist_user
        and (not session.title or session.title == "(tanpa judul)")
    ):
        # Generate a short title in the background using the AI.
        asyncio.create_task(
            _auto_generate_title(
                context, db_path, sid, user_message, reply, model
            )
        )


async def chat_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or update.effective_user is None:
        return
    text = (update.message.text or "").strip()
    if not text:
        return
    # Reply keyboard buttons arrive as text — handled by their own handlers.
    if ui.is_button_label(text):
        return

    user_id = update.effective_user.id
    db_path: str = context.application.bot_data["db_path"]
    default_model: str = context.application.bot_data["default_model"]
    prefs, deny = await _admit(update, context)
    if prefs is None:
        return
    if deny:
        await update.message.reply_text(deny, parse_mode=ParseMode.HTML)
        return

    if not prefs.onboarded:
        await _start_onboarding(update, context, prefs)
        return

    # Handle pending actions first.
    if context.user_data is not None:
        if context.user_data.get(PENDING_RENAME):
            sid = context.user_data.pop(PENDING_RENAME)
            try:
                await db.update_session_title(db_path, int(sid), _make_title(text))
                await update.message.reply_text("Judul sesi diperbarui.")
            except Exception:  # noqa: BLE001
                logger.exception("rename failed")
                await update.message.reply_text("Gagal rename sesi.")
            return
        if context.user_data.get(PENDING_CUSTOM_PROMPT):
            context.user_data.pop(PENDING_CUSTOM_PROMPT)
            await db.upsert_user_prefs(
                db_path, user_id, custom_system_prompt=text, persona="custom"
            )
            await update.message.reply_text(
                "Custom system prompt tersimpan dan persona dipasang ke <b>Custom</b>.",
                parse_mode=ParseMode.HTML,
            )
            return
        if context.user_data.get(PENDING_SEARCH):
            context.user_data.pop(PENDING_SEARCH)
            await _do_search(update, context, text)
            return
        if context.user_data.get(PENDING_QUICK):
            qkey = context.user_data.pop(PENDING_QUICK)
            qp = quick_prompts.get(qkey)
            if qp is not None:
                text = qp.template.format(input=text)

    model = _active_model(prefs, default_model)
    sid = await _get_or_create_active_session(
        db_path, user_id, model, prefs.persona or "default", context
    )
    await _send_ai_reply(
        update, context, sid=sid, user_message=text, persist_user=True
    )


async def regenerate_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    if query is None or query.data is None or update.effective_user is None:
        return
    if not query.data.startswith(ui.REGEN_PREFIX):
        await query.answer()
        return
    try:
        sid = int(query.data[len(ui.REGEN_PREFIX):])
    except ValueError:
        await query.answer()
        return
    prefs, deny = await _admit(update, context)
    if prefs is None:
        await query.answer()
        return
    if deny:
        await query.answer(re.sub(r"<[^>]+>", "", deny), show_alert=True)
        return
    db_path: str = context.application.bot_data["db_path"]
    session = await db.get_session(db_path, sid)
    if session is None or session.user_id != update.effective_user.id:
        await query.answer("Sesi tidak valid.", show_alert=True)
        return
    popped = await db.delete_last_assistant_message(db_path, sid)
    if popped is None:
        await query.answer("Tidak ada jawaban yang bisa diregenerasi.", show_alert=True)
        return
    user_content, _ = popped
    await query.answer("Generating ulang...")
    if query.message is not None:
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except BadRequest:
            pass
    await _send_ai_reply(
        update, context, sid=sid, user_message=user_content, persist_user=True
    )


# --- Auto-title -------------------------------------------------------------

async def _auto_generate_title(
    context: ContextTypes.DEFAULT_TYPE,
    db_path: str,
    sid: int,
    user_message: str,
    assistant_reply: str,
    model: str,
) -> None:
    client: TansAIClient = context.application.bot_data["tans_client"]
    try:
        prompt = (
            "Beri judul singkat (maksimal 6 kata, tanpa tanda kutip, tanpa "
            "tanda baca di akhir) untuk percakapan berikut. Gunakan bahasa "
            "yang sama dengan pesan user.\n\n"
            f"User: {user_message[:500]}\n"
            f"Assistant: {assistant_reply[:500]}\n\n"
            "Judul:"
        )
        title = await client.chat(message=prompt, model=model)
        title = _make_title(title.strip().strip('"\u201c\u201d'))
        if title and title != "(tanpa judul)":
            await db.update_session_title(db_path, sid, title)
    except Exception:  # noqa: BLE001 - title is best-effort
        logger.debug("auto-title failed for session %s", sid, exc_info=True)


# --- Bootstrap --------------------------------------------------------------

async def _post_init(application: Application) -> None:
    cfg = application.bot_data
    db_path = str(cfg["db_path"])
    await db.init_db(db_path)
    logger.info("SQLite chat history at %s", db_path)
    client = TansAIClient(
        base_url=str(cfg["tans_base_url"]),
        api_key=str(cfg["tans_api_key"]),
        timeout=float(cfg["timeout"]),
    )
    application.bot_data["tans_client"] = client
    rl = rate_limiter.RateLimiter(
        per_minute=int(cfg.get("rate_limit_per_minute", 0)),
        per_day=int(cfg.get("rate_limit_per_day", 0)),
        admin_ids=tuple(cfg.get("admin_ids") or ()),
    )
    application.bot_data["rate_limiter"] = rl
    if rl.is_disabled():
        logger.info("Rate limiter disabled (per_minute=0, per_day=0)")
    else:
        logger.info(
            "Rate limiter: %s/min, %s/day", rl.per_minute, rl.per_day
        )
    if cfg.get("waitlist_mode"):
        logger.info("WAITLIST_MODE on — new users default to 'waitlist' status.")


async def _post_shutdown(application: Application) -> None:
    client: TansAIClient | None = application.bot_data.get("tans_client")
    if client is not None:
        await client.aclose()


def build_application() -> Application:
    cfg = _config()
    application = (
        ApplicationBuilder()
        .token(str(cfg["telegram_token"]))
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )
    application.bot_data.update(
        {
            "tans_base_url": cfg["tans_base_url"],
            "tans_api_key": cfg["tans_api_key"],
            "default_model": cfg["default_model"],
            "history_max": cfg["history_max"],
            "timeout": cfg["timeout"],
            "db_path": cfg["db_path"],
            "admin_ids": cfg["admin_ids"],
            "auto_title": cfg["auto_title"],
            "rate_limit_per_minute": cfg["rate_limit_per_minute"],
            "rate_limit_per_day": cfg["rate_limit_per_day"],
            "waitlist_mode": cfg["waitlist_mode"],
        }
    )

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("models", models_command))
    application.add_handler(CommandHandler("model", model_command))
    application.add_handler(CommandHandler("new", new_chat_command))
    application.add_handler(CommandHandler("history", history_command))
    application.add_handler(CommandHandler("reset", reset_command))
    application.add_handler(CommandHandler("settings", settings_command))
    application.add_handler(CommandHandler("persona", persona_command))
    application.add_handler(CommandHandler("quick", quick_command))
    application.add_handler(CommandHandler("find", search_command))
    application.add_handler(CommandHandler("search", search_command))
    application.add_handler(CommandHandler("export", export_command))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CommandHandler("admin", admin_command))
    application.add_handler(CommandHandler("broadcast", broadcast_command))
    application.add_handler(CommandHandler("approve", approve_command))
    application.add_handler(CommandHandler("deny", deny_command))
    application.add_handler(CommandHandler("ban", ban_command))
    application.add_handler(CommandHandler("unban", unban_command))
    application.add_handler(CommandHandler("waitlist", waitlist_command))
    application.add_handler(CommandHandler("users", users_command))
    application.add_handler(CommandHandler("cancel", cancel_command))

    application.add_handler(
        CallbackQueryHandler(model_callback, pattern=f"^{ui.MODEL_PREFIX}")
    )
    application.add_handler(
        CallbackQueryHandler(persona_callback, pattern=f"^{ui.PERSONA_PREFIX}")
    )
    application.add_handler(
        CallbackQueryHandler(quick_callback, pattern=f"^{ui.QUICK_PREFIX}")
    )
    application.add_handler(
        CallbackQueryHandler(load_session_callback, pattern=f"^{ui.SESSION_PREFIX}")
    )
    application.add_handler(
        CallbackQueryHandler(
            history_page_callback, pattern=f"^{ui.SESSION_PAGE_PREFIX}"
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            session_action_callback, pattern=f"^{ui.SESSION_ACTION_PREFIX}"
        )
    )
    application.add_handler(
        CallbackQueryHandler(regenerate_callback, pattern=f"^{ui.REGEN_PREFIX}")
    )
    application.add_handler(
        CallbackQueryHandler(settings_callback, pattern=f"^{ui.SETTINGS_PREFIX}")
    )
    application.add_handler(
        CallbackQueryHandler(language_callback, pattern=f"^{ui.LANG_PREFIX}")
    )
    application.add_handler(
        CallbackQueryHandler(onboarding_callback, pattern=f"^{ui.ONBOARD_PREFIX}")
    )
    application.add_handler(
        CallbackQueryHandler(export_callback, pattern=f"^{ui.EXPORT_PREFIX}")
    )
    application.add_handler(
        CallbackQueryHandler(search_callback, pattern=f"^{ui.SEARCH_PREFIX}")
    )

    # Reply-keyboard buttons arrive as plain text — route each label to its
    # command handler BEFORE the catch-all chat handler.
    button_routes = [
        (ui.BTN_NEW_CHAT, new_chat_command),
        (ui.BTN_HISTORY, history_command),
        (ui.BTN_MODELS, models_command),
        (ui.BTN_QUICK, quick_command),
        (ui.BTN_PERSONA, persona_command),
        (ui.BTN_STATUS, status_command),
        (ui.BTN_SETTINGS, settings_command),
        (ui.BTN_HELP, help_command),
        (ui.BTN_RESET, reset_command),
    ]
    for label, handler in button_routes:
        application.add_handler(
            MessageHandler(
                filters.TEXT & filters.Regex(f"^{re.escape(label)}$"), handler
            )
        )

    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, chat_message))
    return application


def main() -> None:
    logging.basicConfig(
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        level=logging.INFO,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    application = build_application()
    logger.info("Bot starting (long polling)...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
