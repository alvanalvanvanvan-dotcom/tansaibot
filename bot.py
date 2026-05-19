"""Telegram bot that proxies messages to a Tans AI API Gateway.

v2 additions (Tier S):
  - Structured JSON logging via logging_setup (#13)
  - Sentry error tracking (#14)
  - Health + metrics HTTP server (#15)
  - Graceful SIGTERM shutdown (#16)
  - /forgetme — GDPR data deletion (#11)
  - /privacy — strict privacy mode (#30)
  - /save, /useprompt, /myprompts — saved prompt templates (#8)
  - /rotatekey — API key hot-swap (#26)
  - /backup — SQLite backup (#24)
  - /auditlog — admin audit log (#28)
  - Telegram language_code → auto UI language (#50/#12)
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
import shutil
import signal
from pathlib import Path

from dotenv import load_dotenv
from telegram import (
    BotCommand,
    BotCommandScopeAllPrivateChats,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQueryResultArticle,
    InlineQueryResultsButton,
    InputTextMessageContent,
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
    InlineQueryHandler,
    MessageHandler,
    filters,
)

import db
import follow_ups
import personas
import quick_prompts
import rate_limiter
import streaming
import summarizer
import ui
import voice as voice_mod
from health import HealthServer, metrics
from logging_setup import setup_logging
from markdown_utils import chunk_for_telegram, to_telegram_html
from tans_client import TansAIClient, TansAIError

# --- Sentry (optional) (#14) -------------------------------------------------
try:
    import sentry_sdk
    _SENTRY_AVAILABLE = True
except ImportError:
    _SENTRY_AVAILABLE = False

# Tier M imports
import memory
import group as group_mod
from cost_tracker import cost_usd, format_cost, get_fallback_model, get_user_tier, is_model_allowed
from scheduler import ReminderScheduler, parse_reminder_time
from web_search import format_results_for_prompt, search as web_search_fn, summarize_url
from share import generate_session_html
from webhook import get_webhook_config, is_webhook_mode

# Tier L imports
from rag import RAGStore
from tools import get_default_registry
from sandbox import execute_code, format_result as format_sandbox_result
import payment

logger = logging.getLogger(__name__)

# Global RAG store instance (initialized in build_application)
_rag_store: RAGStore | None = None


# Pending-action keys stored in ``context.user_data``.
PENDING_QUICK = "pending_quick"
PENDING_RENAME = "pending_rename"
PENDING_CUSTOM_PROMPT = "pending_custom_prompt"
PENDING_SEARCH = "pending_search"
PENDING_SAVE_PROMPT_NAME = "pending_save_prompt_name"   # (#8)
PENDING_SAVE_PROMPT_CONTENT = "pending_save_prompt_content"  # (#8)

# Per-user-per-chat cache of the most recently rendered AI placeholder
# message id, so the Regenerate button can edit the same message instead
# of spamming new ones.
LAST_AI_MESSAGE = "last_ai_message_id"


# --- Config -----------------------------------------------------------------

def _config() -> dict[str, str | int | float]:
    load_dotenv(override=True)
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
        "follow_ups_enabled": os.getenv("FOLLOW_UPS_ENABLED", "1")
        not in ("0", "false", "False"),
        "summarization_threshold": int(os.getenv("SUMMARIZATION_THRESHOLD", "24")),
        "summarization_keep_last": int(os.getenv("SUMMARIZATION_KEEP_LAST", "10")),
        "summarization_delta": int(os.getenv("SUMMARIZATION_DELTA", "12")),
        "voice_api_base": os.getenv("VOICE_API_BASE", "https://api.openai.com/v1"),
        "voice_api_key": os.getenv("VOICE_API_KEY", ""),
        "voice_stt_model": os.getenv("VOICE_STT_MODEL", "whisper-1"),
        "voice_tts_model": os.getenv("VOICE_TTS_MODEL", "tts-1"),
        "voice_tts_voice": os.getenv("VOICE_TTS_VOICE", "alloy"),
        "voice_timeout": float(os.getenv("VOICE_TIMEOUT", "60")),
        "bot_username": os.getenv("BOT_USERNAME", ""),
        # v2 additions
        "sentry_dsn": os.getenv("SENTRY_DSN", ""),           # (#14)
        "log_level": os.getenv("LOG_LEVEL", "INFO"),          # (#13)
        "health_port": int(os.getenv("HEALTH_PORT", "8081")), # (#15)
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
    prefs = await _ensure_prefs(
        db_path,
        update.effective_user.id,
        default_model,
        waitlist_mode=_waitlist_mode(context),
        admin_ids=_admin_ids(context),
        display_name=_display_name_from_update(update),
    )

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

FOLLOWUP_CACHE = "fup_cache"
FOLLOWUP_CACHE_MAX = 20


def _store_followups(
    context: ContextTypes.DEFAULT_TYPE, message_id: int, suggestions: list[str]
) -> None:
    if context.chat_data is None:
        return
    cache = context.chat_data.setdefault(FOLLOWUP_CACHE, {})
    cache[int(message_id)] = list(suggestions[:3])
    # Bound memory: trim oldest entries.
    if len(cache) > FOLLOWUP_CACHE_MAX:
        for key in sorted(cache.keys())[: len(cache) - FOLLOWUP_CACHE_MAX]:
            cache.pop(key, None)


def _get_followups(
    context: ContextTypes.DEFAULT_TYPE, message_id: int
) -> list[str]:
    if context.chat_data is None:
        return []
    return list(context.chat_data.get(FOLLOWUP_CACHE, {}).get(int(message_id), []))


async def _prepare_context_messages(
    db_path: str,
    client: TansAIClient,
    session: db.Session,
    *,
    threshold: int,
    keep_last: int,
    delta: int,
    history_max: int,
    language: str,
    model: str,
) -> tuple[str, list[db.Message]]:
    """Return (summary_or_empty, recent_messages) to feed into the prompt.

    Triggers a background summary refresh when the session has piled up too
    many new messages since the last snapshot.
    """
    if threshold <= 0:
        history = await db.get_messages(db_path, session.id, limit=history_max)
        return "", history

    total = await db.count_messages(db_path, session.id)
    if total < threshold:
        history = await db.get_messages(db_path, session.id, limit=history_max)
        return session.summary, history

    all_messages = await db.get_all_messages(db_path, session.id)
    summarized_count = 0
    if session.summary and session.summary_until_id:
        for i, m in enumerate(all_messages):
            if m.id > session.summary_until_id:
                summarized_count = i
                break
        else:
            summarized_count = len(all_messages)

    if summarizer.should_summarize(
        total_messages=total,
        summarized_count=summarized_count,
        threshold=threshold,
        keep_last=keep_last,
        delta=delta,
    ):
        to_summarize = summarizer.messages_to_summarize(
            all_messages, summarized_count=summarized_count, keep_last=keep_last
        )
        if to_summarize:
            new_summary = await summarizer.generate_summary(
                client,
                model=model,
                messages=to_summarize,
                previous_summary=session.summary,
                language=language,
            )
            if new_summary and new_summary != session.summary:
                last_id = to_summarize[-1].id
                await db.update_session_summary(
                    db_path, session.id, new_summary, last_id
                )
                logger.info(
                    "Session %s summarized up to message %s (len=%d)",
                    session.id,
                    last_id,
                    len(new_summary),
                )
                recent = all_messages[-keep_last:]
                return new_summary, recent
    if session.summary and session.summary_until_id:
        recent = all_messages[-keep_last:]
        return session.summary, recent
    history = await db.get_messages(db_path, session.id, limit=history_max)
    return "", history


def _build_prompt_with_summary(
    persona_prompt: str,
    summary: str,
    history: list[db.Message],
    new_message: str,
) -> str:
    lines: list[str] = []
    if persona_prompt:
        lines.append(f"System: {persona_prompt}")
    if summary:
        lines.append(f"Conversation summary so far: {summary}")
    for msg in history:
        prefix = "User" if msg.role == "user" else "Assistant"
        lines.append(f"{prefix}: {msg.content}")
    lines.append(f"User: {new_message}")
    lines.append("Assistant:")
    return "\n".join(lines) if lines else new_message


async def _maybe_send_voice_reply(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    chat_id: int,
    text: str,
    prefs: db.UserPrefs,
) -> None:
    if not prefs.tts_enabled:
        return
    vc: voice_mod.VoiceClient | None = context.application.bot_data.get(
        "voice_client"
    )
    if vc is None or not vc.enabled:
        return
    try:
        audio = await vc.synthesize(text)
    except voice_mod.VoiceError as exc:
        logger.debug("TTS failed: %s", exc)
        return
    except Exception:  # noqa: BLE001
        logger.exception("Unexpected TTS failure")
        return
    try:
        await context.bot.send_voice(chat_id=chat_id, voice=audio)
    except Exception:  # noqa: BLE001
        logger.exception("send_voice failed")


async def _attach_followups(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    placeholder_message,
    sid: int,
    rendered_html: str,
    fallback_text: str,
    last_user: str,
    last_assistant: str,
    language: str,
    model: str,
) -> None:
    """Generate 3 follow-up suggestions and re-render the reply with them."""
    client: TansAIClient = context.application.bot_data["tans_client"]
    suggestions = await follow_ups.generate(
        client,
        model=model,
        last_user_message=last_user,
        last_assistant_message=last_assistant,
        language=language,
    )
    if not suggestions:
        return
    _store_followups(context, placeholder_message.message_id, suggestions)
    markup = ui.reply_actions_keyboard(
        sid,
        followups=suggestions,
        followup_msg_id=placeholder_message.message_id,
    )
    try:
        await placeholder_message.edit_reply_markup(reply_markup=markup)
    except BadRequest:
        logger.debug("edit_reply_markup failed", exc_info=True)


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
    follow_ups_enabled: bool = context.application.bot_data.get(
        "follow_ups_enabled", True
    )
    summarization_threshold: int = context.application.bot_data.get(
        "summarization_threshold", 0
    )
    summarization_keep_last: int = context.application.bot_data.get(
        "summarization_keep_last", 10
    )
    summarization_delta: int = context.application.bot_data.get(
        "summarization_delta", 12
    )
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
        summary, history = await _prepare_context_messages(
            db_path,
            client,
            session,
            threshold=summarization_threshold,
            keep_last=summarization_keep_last,
            delta=summarization_delta,
            history_max=history_max,
            language=prefs.ui_language,
            model=model,
        )
        prompt = _build_prompt_with_summary(
            persona_prompt, summary, history, user_message
        )

        # (#32) Inject RAG Context
        if getattr(prefs, "tier", "free") in ("premium", "admin") and _rag_store is not None:
            rag_context = await _rag_store.query(user_id, user_message)
            if rag_context:
                prompt += f"\n\n{rag_context}"

        # (#36) Inject Tools Spec
        tool_registry = get_default_registry()
        prompt += f"\n\n{tool_registry.get_spec_text()}"

        try:
            reply = await client.chat(message=prompt, model=model)
            
            # (#36) Execute tool calls in AI response
            if "TOOL:" in reply:
                reply = await tool_registry.execute_from_response(reply)
                

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
        + _estimate_tokens(summary)
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

    if follow_ups_enabled:
        asyncio.create_task(
            _attach_followups(
                context,
                placeholder_message=placeholder,
                sid=sid,
                rendered_html=rendered_first,
                fallback_text=chunks[0],
                last_user=user_message,
                last_assistant=reply,
                language=prefs.ui_language,
                model=model,
            )
        )

    asyncio.create_task(
        _maybe_send_voice_reply(context, chat_id=chat_id, text=reply, prefs=prefs)
    )

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


async def followup_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    if query is None or query.data is None or update.effective_user is None:
        return
    if not query.data.startswith(ui.FOLLOWUP_PREFIX):
        await query.answer()
        return
    payload = query.data[len(ui.FOLLOWUP_PREFIX):]
    try:
        msg_id_str, idx_str = payload.split(":", 1)
        msg_id = int(msg_id_str)
        idx = int(idx_str)
    except (ValueError, IndexError):
        await query.answer()
        return
    suggestions = _get_followups(context, msg_id)
    if not suggestions or idx < 0 or idx >= len(suggestions):
        await query.answer("Saran sudah kedaluwarsa.", show_alert=True)
        return
    suggestion_text = suggestions[idx]

    prefs, deny = await _admit(update, context)
    if prefs is None:
        await query.answer()
        return
    if deny:
        await query.answer(re.sub(r"<[^>]+>", "", deny), show_alert=True)
        return

    db_path: str = context.application.bot_data["db_path"]
    default_model: str = context.application.bot_data["default_model"]
    user_id = update.effective_user.id
    model = _active_model(prefs, default_model)
    sid = await _get_or_create_active_session(
        db_path, user_id, model, prefs.persona or "default", context
    )
    await query.answer()
    if query.message is not None:
        try:
            await query.edit_message_reply_markup(
                reply_markup=ui.reply_actions_keyboard(sid)
            )
        except BadRequest:
            pass
    if update.effective_chat is not None:
        try:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=f"\u2192 {suggestion_text}",
            )
        except Exception:  # noqa: BLE001
            logger.debug("echo follow-up failed", exc_info=True)
    await _send_ai_reply(
        update, context, sid=sid, user_message=suggestion_text, persist_user=True
    )


# --- /tts toggle ------------------------------------------------------------

async def tts_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or update.effective_user is None:
        return
    db_path: str = context.application.bot_data["db_path"]
    default_model: str = context.application.bot_data["default_model"]
    user_id = update.effective_user.id
    prefs = await _ensure_prefs(db_path, user_id, default_model)
    vc: voice_mod.VoiceClient | None = context.application.bot_data.get(
        "voice_client"
    )
    if vc is None or not vc.enabled:
        await update.message.reply_text(
            "Voice belum dikonfigurasi di bot ini. Admin harus set VOICE_API_KEY."
        )
        return
    args = context.args or []
    if args and args[0].lower() in ("on", "1", "true", "enable"):
        new_value = True
    elif args and args[0].lower() in ("off", "0", "false", "disable"):
        new_value = False
    else:
        new_value = not prefs.tts_enabled
    await db.upsert_user_prefs(db_path, user_id, tts_enabled=new_value)
    status = "AKTIF \U0001f50a" if new_value else "NONAKTIF \U0001f507"
    await update.message.reply_text(
        f"Text-to-speech sekarang: {status}.\n"
        "Setiap jawaban AI akan dikirim juga sebagai voice message."
        if new_value
        else f"Text-to-speech sekarang: {status}.\nJawaban AI hanya teks."
    )


# --- Voice messages (STT) ---------------------------------------------------

async def voice_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if update.message is None or update.effective_user is None:
        return
    msg = update.message
    media = msg.voice or msg.audio
    if media is None:
        return
    vc: voice_mod.VoiceClient | None = context.application.bot_data.get(
        "voice_client"
    )
    if vc is None or not vc.enabled:
        await msg.reply_text(
            "Voice belum dikonfigurasi. Kirim pertanyaan dalam bentuk teks ya."
        )
        return

    prefs, deny = await _admit(update, context)
    if prefs is None:
        return
    if deny:
        await msg.reply_text(deny, parse_mode=ParseMode.HTML)
        return

    chat_id = update.effective_chat.id if update.effective_chat else update.effective_user.id
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

    file = await context.bot.get_file(media.file_id)
    audio_bytes = await file.download_as_bytearray()
    suffix = ".ogg" if msg.voice is not None else ".m4a"
    try:
        transcript = await vc.transcribe(bytes(audio_bytes), filename=f"voice{suffix}")
    except voice_mod.VoiceError as exc:
        await msg.reply_text(f"Gagal transkrip: {exc}")
        return
    transcript = (transcript or "").strip()
    if not transcript:
        await msg.reply_text("Maaf, tidak ada teks yang berhasil ditranskrip.")
        return

    await msg.reply_text(
        f"\U0001f3a4 <i>{ui.escape(transcript)}</i>",
        parse_mode=ParseMode.HTML,
    )

    db_path: str = context.application.bot_data["db_path"]
    default_model: str = context.application.bot_data["default_model"]
    user_id = update.effective_user.id
    model = _active_model(prefs, default_model)
    sid = await _get_or_create_active_session(
        db_path, user_id, model, prefs.persona or "default", context
    )
    await _send_ai_reply(
        update, context, sid=sid, user_message=transcript, persist_user=True
    )


# --- Inline mode ------------------------------------------------------------

async def inline_query(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    iq = update.inline_query
    if iq is None or update.effective_user is None:
        return
    query_text = (iq.query or "").strip()
    if len(query_text) < 3:
        await iq.answer(
            results=[],
            cache_time=1,
            is_personal=True,
            button=InlineQueryResultsButton(
                text="Tulis pertanyaan minimal 3 huruf",
                start_parameter="inline",
            ),
        )
        return

    db_path: str = context.application.bot_data["db_path"]
    default_model: str = context.application.bot_data["default_model"]
    rl: rate_limiter.RateLimiter = context.application.bot_data["rate_limiter"]
    user_id = update.effective_user.id
    prefs = await _ensure_prefs(db_path, user_id, default_model)

    if prefs.status == db.STATUS_BANNED:
        await iq.answer(
            results=[],
            cache_time=1,
            is_personal=True,
            button=InlineQueryResultsButton(
                text="Akses Anda diblokir admin.",
                start_parameter="banned",
            ),
        )
        return
    if prefs.status == db.STATUS_WAITLIST:
        await iq.answer(
            results=[],
            cache_time=1,
            is_personal=True,
            button=InlineQueryResultsButton(
                text="Akun Anda masih waitlist. Buka chat untuk info.",
                start_parameter="waitlist",
            ),
        )
        return
    rl_result = await rl.check_and_consume(user_id)
    if not rl_result.allowed:
        await iq.answer(
            results=[],
            cache_time=2,
            is_personal=True,
            button=InlineQueryResultsButton(
                text="Rate limit tercapai, coba lagi nanti.",
                start_parameter="rate",
            ),
        )
        return

    client: TansAIClient = context.application.bot_data["tans_client"]
    model = _active_model(prefs, default_model)
    persona_prompt = personas.resolve_system_prompt(
        prefs.persona or "default", prefs.custom_system_prompt
    )
    prompt = _build_prompt_with_summary(persona_prompt, "", [], query_text)
    try:
        reply = await asyncio.wait_for(
            client.chat(message=prompt, model=model), timeout=20.0
        )
    except (asyncio.TimeoutError, TansAIError):
        await iq.answer(
            results=[],
            cache_time=1,
            is_personal=True,
            button=InlineQueryResultsButton(
                text="AI lambat / error. Coba lagi.",
                start_parameter="err",
            ),
        )
        return
    except Exception:  # noqa: BLE001
        logger.exception("inline query failed")
        return
    reply = (reply or "").strip() or "(AI mengembalikan response kosong.)"
    preview = reply.replace("\n", " ")[:120]
    result = InlineQueryResultArticle(
        id=f"q{abs(hash(query_text)) % 10**12}",
        title=preview or query_text,
        description=query_text[:80],
        input_message_content=InputTextMessageContent(
            message_text=reply[:4000]
        ),
    )
    try:
        await iq.answer(results=[result], cache_time=5, is_personal=True)
    except Exception:  # noqa: BLE001
        logger.exception("answer_inline_query failed")


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

PUBLIC_BOT_COMMANDS: list[tuple[str, str]] = [
    ("start", "Mulai bot & buka onboarding"),
    ("new", "Mulai chat baru"),
    ("history", "Lihat & kelola riwayat sesi"),
    ("search", "Cari di riwayat chat"),
    ("models", "Pilih model AI"),
    ("persona", "Pilih persona / mode AI"),
    ("quick", "Template prompt cepat"),
    ("settings", "Buka menu pengaturan"),
    ("export", "Export sesi ke Markdown"),
    ("tts", "On/off voice reply (TTS)"),
    ("stats", "Statistik penggunaan Anda"),
    ("status", "Cek koneksi ke Tans AI"),
    ("reset", "Reset preferensi ke default"),
    ("help", "Bantuan & daftar perintah"),
    ("cancel", "Batalkan operasi yang berjalan"),
]


async def _publish_bot_commands(application: Application) -> None:
    commands = [BotCommand(name, desc) for name, desc in PUBLIC_BOT_COMMANDS]
    try:
        await application.bot.set_my_commands(
            commands, scope=BotCommandScopeAllPrivateChats()
        )
        logger.info("Published %d bot commands via setMyCommands", len(commands))
    except Exception:  # noqa: BLE001
        logger.exception("set_my_commands failed")


async def _post_init(application: Application) -> None:
    cfg = application.bot_data
    db_path = str(cfg["db_path"])
    await db.init_db(db_path)
    logger.info("SQLite chat history at %s", db_path)

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
        logger.info("WAITLIST_MODE on \u2014 new users default to 'waitlist' status.")
    voice_cfg = voice_mod.VoiceConfig(
        api_base=str(cfg.get("voice_api_base", "")),
        api_key=str(cfg.get("voice_api_key", "")),
        stt_model=str(cfg.get("voice_stt_model", "whisper-1")),
        tts_model=str(cfg.get("voice_tts_model", "tts-1")),
        tts_voice=str(cfg.get("voice_tts_voice", "alloy")),
        timeout=float(cfg.get("voice_timeout", 60.0)),
    )
    vc = voice_mod.VoiceClient(voice_cfg)
    application.bot_data["voice_client"] = vc
    if vc.enabled:
        logger.info(
            "Voice enabled (base=%s, stt=%s, tts=%s/%s)",
            voice_cfg.api_base,
            voice_cfg.stt_model,
            voice_cfg.tts_model,
            voice_cfg.tts_voice,
        )
    else:
        logger.info("Voice disabled (VOICE_API_KEY not set)")
    await _publish_bot_commands(application)


async def _post_shutdown(application: Application) -> None:
    client: TansAIClient | None = application.bot_data.get("tans_client")
    if client is not None:
        await client.aclose()
    vc: voice_mod.VoiceClient | None = application.bot_data.get("voice_client")
    if vc is not None:
        await vc.aclose()


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
            "follow_ups_enabled": cfg["follow_ups_enabled"],
            "summarization_threshold": cfg["summarization_threshold"],
            "summarization_keep_last": cfg["summarization_keep_last"],
            "summarization_delta": cfg["summarization_delta"],
            "voice_api_base": cfg["voice_api_base"],
            "voice_api_key": cfg["voice_api_key"],
            "voice_stt_model": cfg["voice_stt_model"],
            "voice_tts_model": cfg["voice_tts_model"],
            "voice_tts_voice": cfg["voice_tts_voice"],
            "voice_timeout": cfg["voice_timeout"],
            "bot_username": cfg["bot_username"],
        }
    )
    
    # Initialize TansAIClient synchronously so it's available immediately in bot_data
    client = TansAIClient(
        base_url=str(cfg["tans_base_url"]),
        api_key=str(cfg["tans_api_key"]),
        timeout=float(cfg["timeout"]),
    )
    application.bot_data["tans_client"] = client

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
    application.add_handler(CommandHandler("tts", tts_command))
    application.add_handler(CommandHandler("cancel", cancel_command))
    # v2 Tier-S handlers
    application.add_handler(CommandHandler("forgetme", forgetme_command))       # (#11)
    application.add_handler(CommandHandler("privacy", privacy_command))         # (#30)
    application.add_handler(CommandHandler("save", save_prompt_command))        # (#8)
    application.add_handler(CommandHandler("useprompt", use_prompt_command))    # (#8)
    application.add_handler(CommandHandler("myprompts", my_prompts_command))    # (#8)
    application.add_handler(CommandHandler("rotatekey", rotatekey_command))     # (#26)
    application.add_handler(CommandHandler("backup", backup_command))           # (#24)
    application.add_handler(CommandHandler("auditlog", auditlog_command))       # (#28)
    # v2 Tier-M handlers
    application.add_handler(CommandHandler("insights", insights_command))       # (#18)
    application.add_handler(CommandHandler("remember", remember_command))       # (#35)
    application.add_handler(CommandHandler("memories", memories_command))       # (#35)
    application.add_handler(CommandHandler("forget", forget_memory_command))    # (#35)
    application.add_handler(CommandHandler("search_web", web_search_command))   # (#38)
    application.add_handler(CommandHandler("websearch", web_search_command))    # (#38)
    application.add_handler(CommandHandler("summarize", summarize_url_command)) # (#34)
    application.add_handler(CommandHandler("remind", remind_command))           # (#40)
    application.add_handler(CommandHandler("share", share_command))             # (#6)
    application.add_handler(CommandHandler("cost", cost_command))               # (#44)
    application.add_handler(CommandHandler("settier", set_tier_command))        # (#45)
    application.add_handler(
        MessageHandler(filters.PHOTO, photo_message)                             # (#31)
    )
    application.add_handler(
        MessageHandler(filters.Document.ALL & ~filters.PHOTO, document_message) # (#31, #32)
    )
    
    # v2 Tier-L handlers
    application.add_handler(CommandHandler("rag", rag_command))                 # (#32)
    application.add_handler(CommandHandler("python", python_command))           # (#37)
    application.add_handler(CommandHandler("premium", premium_command))         # (#47)
    
    from telegram.ext import PreCheckoutQueryHandler
    application.add_handler(PreCheckoutQueryHandler(payment.handle_pre_checkout))
    application.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, payment.handle_successful_payment))
    application.add_handler(CallbackQueryHandler(buy_premium_callback, pattern="^buy_premium_"))

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
    application.add_handler(
        CallbackQueryHandler(followup_callback, pattern=f"^{ui.FOLLOWUP_PREFIX}")
    )
    application.add_handler(InlineQueryHandler(inline_query))

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

    application.add_handler(
        MessageHandler(filters.VOICE | filters.AUDIO, voice_message)
    )
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, chat_message))
    return application


def main() -> None:
    cfg = _config()

    # (#13) Structured logging
    setup_logging(str(cfg.get("log_level", "INFO")))

    # (#14) Sentry error tracking
    sentry_dsn = str(cfg.get("sentry_dsn", ""))
    if sentry_dsn and _SENTRY_AVAILABLE:
        sentry_sdk.init(
            dsn=sentry_dsn,
            traces_sample_rate=0.1,
        )
        logger.info("Sentry initialized")

    application = build_application()
    db_path: str = application.bot_data["db_path"]
    tans_client: TansAIClient = application.bot_data["tans_client"]

    # (#15) Health server
    health_server = HealthServer(
        db_path=db_path,
        tans_client=tans_client,
        port=int(cfg.get("health_port", 8081)),
    )

    # (#40) Reminder scheduler
    reminder_scheduler = ReminderScheduler(application.bot)
    application.bot_data["reminder_scheduler"] = reminder_scheduler

    # (#32) Initialize RAG store globally
    global _rag_store
    _rag_store = RAGStore(db_path)

    async def _run() -> None:
        await health_server.start()
        await reminder_scheduler.start()
        logger.info("Bot starting...")

        # (#21) Webhook or long polling
        webhook_cfg = get_webhook_config()
        async with application:
            await application.start()

            if webhook_cfg:
                logger.info("Webhook mode: %s", webhook_cfg["url"])
                await application.updater.start_webhook(
                    listen=webhook_cfg["listen"],
                    port=webhook_cfg["port"],
                    url_path=webhook_cfg["path"],
                    webhook_url=webhook_cfg["url"],
                    secret_token=webhook_cfg.get("secret_token") or None,
                    cert=webhook_cfg.get("cert"),
                    key=webhook_cfg.get("key"),
                    allowed_updates=Update.ALL_TYPES,
                )
            else:
                logger.info("Long polling mode")
                await application.updater.start_polling(allowed_updates=Update.ALL_TYPES)

            # (#16) Graceful shutdown on SIGTERM / SIGINT
            stop_event = asyncio.Event()

            def _handle_signal() -> None:
                logger.info("Shutdown signal received, stopping gracefully...")
                stop_event.set()

            loop = asyncio.get_event_loop()
            for sig in (signal.SIGTERM, signal.SIGINT):
                try:
                    loop.add_signal_handler(sig, _handle_signal)
                except (NotImplementedError, RuntimeError):
                    pass

            try:
                await stop_event.wait()
            except asyncio.CancelledError:
                pass
            finally:
                logger.info("Stopping...")
                if application.updater and application.updater.running:
                    await application.updater.stop()
                if application.running:
                    await application.stop()
        await health_server.stop()
        await reminder_scheduler.stop()
        await tans_client.aclose()
        logger.info("Bot stopped cleanly.")

    asyncio.run(_run())




# ============================================================================
# v2 Tier-S Command Handlers
# ============================================================================

# --- #11 /forgetme -----------------------------------------------------------

async def forgetme_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """GDPR: permanently delete all user data from this bot."""
    assert update.message is not None
    assert update.effective_user is not None
    user_id = update.effective_user.id
    db_path: str = context.application.bot_data["db_path"]

    # Confirm step: require user to type /forgetme confirm
    args = context.args or []
    if "confirm" not in args:
        await update.message.reply_text(
            "⚠️ <b>Hapus Semua Data?</b>\n\n"
            "Ini akan menghapus SELURUH riwayat percakapan, preferensi, "
            "dan data kamu dari bot ini secara permanen.\n\n"
            "Untuk konfirmasi, kirim:\n"
            "<code>/forgetme confirm</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    await db.forget_user(db_path, user_id)
    if context.user_data is not None:
        context.user_data.clear()
    await update.message.reply_text(
        "✅ Semua data kamu telah dihapus dari bot ini.\n"
        "Kamu bisa mulai dari awal dengan /start.",
    )
    logger.info("forgetme: user %d deleted all their data", user_id)


# --- #30 /privacy ------------------------------------------------------------

async def privacy_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Toggle privacy mode: normal (default) vs strict (no history saved)."""
    assert update.message is not None
    assert update.effective_user is not None
    user_id = update.effective_user.id
    db_path: str = context.application.bot_data["db_path"]
    default_model: str = context.application.bot_data["default_model"]
    prefs = await _ensure_prefs(db_path, user_id, default_model)

    args = context.args or []
    if not args:
        current = getattr(prefs, "privacy_mode", "normal")
        await update.message.reply_text(
            f"🔒 <b>Privacy Mode</b>\n"
            f"Mode saat ini: <code>{current}</code>\n\n"
            "• <code>/privacy normal</code> — simpan riwayat chat (default)\n"
            "• <code>/privacy strict</code> — tidak simpan chat sama sekali (hanya in-memory)",
            parse_mode=ParseMode.HTML,
        )
        return

    mode = args[0].lower()
    if mode not in ("normal", "strict"):
        await update.message.reply_text("Mode tidak valid. Gunakan: normal atau strict")
        return

    await db.upsert_user_prefs(db_path, user_id, privacy_mode=mode)
    emoji = "🔒" if mode == "strict" else "🔓"
    await update.message.reply_text(
        f"{emoji} Privacy mode diubah ke <code>{mode}</code>.\n"
        + ("Pesan tidak akan disimpan ke database mulai sekarang."
           if mode == "strict" else "Riwayat chat akan disimpan secara normal."),
        parse_mode=ParseMode.HTML,
    )


# --- #8 /save /useprompt /myprompts -----------------------------------------

async def save_prompt_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Save a custom prompt template: /save <name> <prompt text>"""
    assert update.message is not None
    assert update.effective_user is not None
    user_id = update.effective_user.id
    db_path: str = context.application.bot_data["db_path"]

    args = context.args or []
    if len(args) < 2:
        await update.message.reply_text(
            "📝 <b>Simpan Prompt</b>\n"
            "Format: <code>/save nama_prompt isi prompt kamu disini</code>\n\n"
            "Contoh:\n"
            "<code>/save ringkas Ringkas teks berikut dalam 3 poin: {teks}</code>\n\n"
            "Panggil dengan /useprompt nama_prompt",
            parse_mode=ParseMode.HTML,
        )
        return

    name = args[0].lower().strip()
    content = " ".join(args[1:])
    await db.save_prompt(db_path, user_id, name, content)
    await update.message.reply_text(
        f"✅ Prompt <code>{ui.escape(name)}</code> disimpan!\n"
        f"Isi: <i>{ui.escape(content[:200])}</i>\n\n"
        "Panggil dengan: <code>/useprompt " + ui.escape(name) + "</code>",
        parse_mode=ParseMode.HTML,
    )


async def use_prompt_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Use a saved prompt: /useprompt <name> [input]"""
    assert update.message is not None
    assert update.effective_user is not None
    user_id = update.effective_user.id
    db_path: str = context.application.bot_data["db_path"]

    args = context.args or []
    if not args:
        await update.message.reply_text(
            "Format: <code>/useprompt nama_prompt [input opsional]</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    name = args[0].lower()
    prompts = await db.list_saved_prompts(db_path, user_id)
    found = next((p for p in prompts if p.name == name), None)
    if found is None:
        await update.message.reply_text(
            f"❌ Prompt <code>{ui.escape(name)}</code> tidak ditemukan.\n"
            "Lihat daftar prompt: /myprompts",
            parse_mode=ParseMode.HTML,
        )
        return

    user_input = " ".join(args[1:]) if len(args) > 1 else ""
    final_prompt = found.content
    if "{teks}" in final_prompt or "{input}" in final_prompt:
        final_prompt = final_prompt.replace("{teks}", user_input).replace("{input}", user_input)
    elif user_input:
        final_prompt = final_prompt + "\n\n" + user_input

    # Inject as pending message
    if context.user_data is None:
        return
    context.user_data["_injected_message"] = final_prompt
    await update.message.reply_text(
        f"⚡ Menjalankan prompt <b>{ui.escape(name)}</b>...\n"
        f"<i>{ui.escape(final_prompt[:200])}</i>",
        parse_mode=ParseMode.HTML,
    )
    # Fake a chat message trigger
    await chat_message(
        update._replace(message=update.message._replace(text=final_prompt)),  # type: ignore[attr-defined]
        context,
    ) if hasattr(update, "_replace") else None  # type: ignore[attr-defined]


async def my_prompts_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List saved prompts for this user."""
    assert update.message is not None
    assert update.effective_user is not None
    db_path: str = context.application.bot_data["db_path"]
    user_id = update.effective_user.id

    prompts = await db.list_saved_prompts(db_path, user_id)
    if not prompts:
        await update.message.reply_text(
            "📭 Belum ada prompt tersimpan.\n"
            "Simpan dengan: <code>/save nama isi prompt</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    lines = ["📋 <b>Prompt Tersimpan:</b>\n"]
    for p in prompts:
        lines.append(
            f"• <code>/useprompt {ui.escape(p.name)}</code>\n"
            f"  <i>{ui.escape(p.content[:80])}{'...' if len(p.content) > 80 else ''}</i>"
        )
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


# --- #26 /rotatekey ----------------------------------------------------------

async def rotatekey_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin: hot-swap Tans AI API key without restart."""
    assert update.message is not None
    assert update.effective_user is not None
    if not _is_admin(context, update.effective_user.id):
        await update.message.reply_text("🚫 Hanya admin yang bisa rotate API key.")
        return

    args = context.args or []
    if not args:
        await update.message.reply_text(
            "Format: <code>/rotatekey tans_newKeyHere</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    new_key = args[0].strip()
    client: TansAIClient = context.application.bot_data["tans_client"]
    old_key = client.api_key
    client.api_key = new_key
    # Rebuild the internal httpx client with new auth headers
    if client._client is not None:
        await client.aclose()

    await db.log_audit(
        context.application.bot_data["db_path"],
        admin_id=update.effective_user.id,
        action="rotatekey",
        detail=f"key rotated (old prefix: {old_key[:8]}...)",
    )

    await update.message.reply_text(
        f"🔑 API key berhasil diperbarui (live, tanpa restart).\n"
        f"Prefix baru: <code>{ui.escape(new_key[:8])}...</code>",
        parse_mode=ParseMode.HTML,
    )
    logger.info(
        "rotatekey: admin %d rotated API key", update.effective_user.id
    )


# --- #24 /backup -------------------------------------------------------------

async def backup_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin: download a SQLite backup as a Telegram document."""
    assert update.message is not None
    assert update.effective_user is not None
    if not _is_admin(context, update.effective_user.id):
        await update.message.reply_text("🚫 Hanya admin.")
        return

    db_path = Path(context.application.bot_data["db_path"])
    if not db_path.exists():
        await update.message.reply_text("❌ File database tidak ditemukan.")
        return

    backup_path = db_path.parent / (db_path.stem + "_backup.db")
    try:
        await asyncio.to_thread(shutil.copy2, db_path, backup_path)
        with open(backup_path, "rb") as f:
            await update.message.reply_document(
                document=f,
                filename=backup_path.name,
                caption=f"📦 Backup database — {backup_path.stat().st_size // 1024} KB",
            )
    except Exception as exc:
        await update.message.reply_text(f"❌ Backup gagal: {exc}")
    finally:
        if backup_path.exists():
            backup_path.unlink(missing_ok=True)

    await db.log_audit(
        str(db_path),
        admin_id=update.effective_user.id,
        action="backup",
        detail="manual backup via /backup",
    )


# --- #28 /auditlog -----------------------------------------------------------

async def auditlog_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin: show recent audit log."""
    assert update.message is not None
    assert update.effective_user is not None
    if not _is_admin(context, update.effective_user.id):
        await update.message.reply_text("🚫 Hanya admin.")
        return

    db_path: str = context.application.bot_data["db_path"]
    entries = await db.get_audit_log(db_path, limit=20)
    if not entries:
        await update.message.reply_text("📋 Audit log masih kosong.")
        return

    lines = ["📋 <b>Audit Log (20 terakhir):</b>\n"]
    for e in entries:
        target = f" → user {e.target_id}" if e.target_id else ""
        lines.append(
            f"• <code>{e.ts[:16]}</code> admin {e.admin_id}: "
            f"<b>{ui.escape(e.action)}</b>{ui.escape(target)}\n"
            f"  <i>{ui.escape(e.detail[:100])}</i>"
        )
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


# ============================================================================
# v2 Tier-M Command Handlers
# ============================================================================

# --- #18 /insights -----------------------------------------------------------

async def insights_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show personal usage analytics."""
    assert update.message is not None
    assert update.effective_user is not None
    user_id = update.effective_user.id
    db_path: str = context.application.bot_data["db_path"]

    data = await db.user_analytics(db_path, user_id)
    tokens_total = data["tokens_in"] + data["tokens_out"]
    default_model = context.application.bot_data["default_model"]
    prefs = await _ensure_prefs(db_path, user_id, default_model)

    # Estimate cost
    cost = 0.0
    for model_name, _ in data["top_models"]:
        # rough split 50/50 in/out
        cost += cost_usd(model_name, data["tokens_in"] // 2, data["tokens_out"] // 2)

    # Activity bar chart
    activity_lines = []
    if data["daily_activity"]:
        max_c = max(c for _, c in data["daily_activity"]) or 1
        for day, count in reversed(data["daily_activity"]):
            bar = "█" * max(1, int(count / max_c * 10))
            activity_lines.append(f"  {day}: {bar} ({count})")

    model_lines = "\n".join(
        f"  {i+1}. <code>{ui.escape(m)}</code> — {c} requests"
        for i, (m, c) in enumerate(data["top_models"])
    ) or "  (tidak ada data)"

    tier = getattr(prefs, "tier", "free")
    text = (
        f"📊 <b>Insight Penggunaan</b> — {ui.escape(prefs.display_name or str(user_id))}\n"
        f"Tier: <code>{tier}</code>\n\n"
        f"💬 Sesi: <b>{data['sessions']}</b> &nbsp;·&nbsp; "
        f"Pesan: <b>{data['messages']}</b>\n"
        f"🔢 Token: <b>{tokens_total:,}</b> "
        f"(in: {data['tokens_in']:,} / out: {data['tokens_out']:,})\n"
        f"💰 Estimasi biaya: <b>{format_cost(cost)}</b>\n\n"
        f"🤖 <b>Top Model:</b>\n{model_lines}\n"
    )
    if activity_lines:
        text += "\n📅 <b>Aktivitas 7 hari terakhir:</b>\n" + "\n".join(activity_lines)

    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


# --- #35 /remember /memories /forget -----------------------------------------

async def remember_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Store a long-term memory fact: /remember <fact>"""
    assert update.message is not None
    assert update.effective_user is not None
    user_id = update.effective_user.id
    db_path: str = context.application.bot_data["db_path"]
    default_model: str = context.application.bot_data["default_model"]
    prefs = await _ensure_prefs(db_path, user_id, default_model)

    args = context.args or []
    if not args:
        await update.message.reply_text(
            "🧠 <b>Ingat Fakta</b>\n"
            "Format: <code>/remember fakta tentang kamu</code>\n\n"
            "Contoh:\n"
            "<code>/remember Saya seorang developer Python dari Jakarta</code>\n\n"
            "AI akan mengingat ini di semua percakapan. Lihat: /memories",
            parse_mode=ParseMode.HTML,
        )
        return

    fact = " ".join(args)
    raw = getattr(prefs, "long_term_memory", "[]")
    new_raw, is_dup = memory.add_memory(raw, fact)

    if is_dup:
        await update.message.reply_text(
            f"ℹ️ Fakta ini sudah tersimpan sebelumnya.",
        )
        return

    await db.upsert_user_prefs(db_path, user_id, long_term_memory=new_raw)
    count = len(memory.list_memories(new_raw))
    await update.message.reply_text(
        f"🧠 Fakta disimpan! Total ingatan: <b>{count}</b>\n"
        f"<i>{ui.escape(fact[:200])}</i>",
        parse_mode=ParseMode.HTML,
    )


async def memories_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List all long-term memories."""
    assert update.message is not None
    assert update.effective_user is not None
    user_id = update.effective_user.id
    db_path: str = context.application.bot_data["db_path"]
    default_model: str = context.application.bot_data["default_model"]
    prefs = await _ensure_prefs(db_path, user_id, default_model)

    raw = getattr(prefs, "long_term_memory", "[]")
    items = memory.list_memories(raw)
    if not items:
        await update.message.reply_text(
            "🧠 Belum ada ingatan. Simpan dengan /remember"
        )
        return

    lines = ["🧠 <b>Ingatanmu:</b>\n"]
    for i, item in enumerate(items, 1):
        lines.append(f"{i}. {ui.escape(item)}")
    lines.append("\n<i>Hapus dengan /forget &lt;nomor&gt;</i>")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def forget_memory_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Delete a memory by index: /forget <number>"""
    assert update.message is not None
    assert update.effective_user is not None
    user_id = update.effective_user.id
    db_path: str = context.application.bot_data["db_path"]
    default_model: str = context.application.bot_data["default_model"]
    prefs = await _ensure_prefs(db_path, user_id, default_model)

    args = context.args or []
    if not args or not args[0].isdigit():
        await update.message.reply_text(
            "Format: <code>/forget &lt;nomor&gt;</code> (lihat /memories)",
            parse_mode=ParseMode.HTML,
        )
        return

    idx = int(args[0])
    raw = getattr(prefs, "long_term_memory", "[]")
    new_raw, ok = memory.remove_memory(raw, idx)
    if not ok:
        await update.message.reply_text(f"❌ Nomor {idx} tidak valid.")
        return
    await db.upsert_user_prefs(db_path, user_id, long_term_memory=new_raw)
    await update.message.reply_text(f"✅ Ingatan #{idx} dihapus.")


# --- #38 /websearch ----------------------------------------------------------

async def web_search_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Search the web and let AI summarize: /websearch <query>"""
    assert update.message is not None
    assert update.effective_user is not None

    prefs, deny = await _admit(update, context)
    if prefs is None or deny:
        if deny:
            await update.message.reply_text(deny, parse_mode=ParseMode.HTML)
        return

    args = context.args or []
    if not args:
        await update.message.reply_text(
            "🌐 Format: <code>/websearch pertanyaan kamu</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    query = " ".join(args)
    placeholder = await update.message.reply_text(f"🔍 Mencari: <i>{ui.escape(query)}</i>...", parse_mode=ParseMode.HTML)

    results = await web_search_fn(query, max_results=3)
    context_block = format_results_for_prompt(results, query)

    db_path: str = context.application.bot_data["db_path"]
    default_model: str = context.application.bot_data["default_model"]
    user_id = update.effective_user.id
    model = _active_model(prefs, default_model)

    prompt = f"{context_block}\n\nBerdasarkan hasil pencarian di atas, jawab: {query}"

    client: TansAIClient = context.application.bot_data["tans_client"]
    try:
        reply = await client.chat(message=prompt, model=model)
    except TansAIError as exc:
        await placeholder.edit_text(f"❌ {exc}")
        return

    from markdown_utils import to_telegram_html
    try:
        await placeholder.edit_text(
            f"🌐 <b>Web Search:</b> {ui.escape(query)}\n\n{to_telegram_html(reply)}",
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
    except Exception:
        await placeholder.edit_text(reply)

    # Show source URLs
    if results:
        url_lines = "\n".join(
            f"• <a href=\"{r['url']}\">{ui.escape(r['title'][:60])}</a>"
            for r in results if r.get("url")
        )
        if url_lines:
            try:
                await update.message.reply_text(
                    f"📎 <b>Sumber:</b>\n{url_lines}",
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                )
            except Exception:
                pass


# --- #34 /summarize <url> ----------------------------------------------------

async def summarize_url_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Summarize a URL: /summarize https://..."""
    assert update.message is not None
    assert update.effective_user is not None

    prefs, deny = await _admit(update, context)
    if prefs is None or deny:
        if deny:
            await update.message.reply_text(deny, parse_mode=ParseMode.HTML)
        return

    args = context.args or []
    if not args:
        await update.message.reply_text(
            "🔗 Format: <code>/summarize https://url-artikel.com</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    url = args[0].strip()
    if not url.startswith("http"):
        url = "https://" + url

    placeholder = await update.message.reply_text(f"⏳ Mengambil konten dari URL...")

    page_text = await summarize_url(url)
    if page_text.startswith("Gagal"):
        await placeholder.edit_text(f"❌ {page_text}")
        return

    db_path: str = context.application.bot_data["db_path"]
    default_model: str = context.application.bot_data["default_model"]
    model = _active_model(prefs, default_model)
    client: TansAIClient = context.application.bot_data["tans_client"]

    lang = prefs.ui_language
    prompt = (
        f"Ringkas artikel berikut dalam {'bahasa Indonesia' if lang == 'id' else 'English'}, "
        f"poin-poin utama, maksimal 5 paragraf:\n\n{page_text}"
    )

    try:
        reply = await client.chat(message=prompt, model=model)
    except TansAIError as exc:
        await placeholder.edit_text(f"❌ {exc}")
        return

    from markdown_utils import to_telegram_html
    try:
        await placeholder.edit_text(
            f"🔗 <b>Ringkasan:</b> <a href=\"{url}\">{url[:60]}</a>\n\n{to_telegram_html(reply)}",
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
    except Exception:
        await placeholder.edit_text(f"Ringkasan {url}:\n\n{reply}")


# --- #40 /remind -------------------------------------------------------------

async def remind_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Set a reminder: /remind 30 menit lagi beli kopi"""
    assert update.message is not None
    assert update.effective_user is not None

    args = context.args or []
    if len(args) < 2:
        await update.message.reply_text(
            "⏰ <b>Pengingat</b>\n"
            "Format: <code>/remind &lt;waktu&gt; &lt;pesan&gt;</code>\n\n"
            "Contoh:\n"
            "<code>/remind 30 menit lagi meeting</code>\n"
            "<code>/remind besok 09:00 backup server</code>\n"
            "<code>/remind 2 jam lagi minum obat</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    user_id = update.effective_user.id
    chat_id = update.effective_chat.id if update.effective_chat else user_id
    db_path: str = context.application.bot_data["db_path"]

    # Try to parse time from first 1-3 words
    time_text = " ".join(args[:3])
    remind_dt = parse_reminder_time(time_text)

    if remind_dt is None:
        # Fallback: try first 2 words
        time_text = " ".join(args[:2])
        remind_dt = parse_reminder_time(time_text)

    if remind_dt is None:
        await update.message.reply_text(
            "❌ Tidak bisa membaca waktu. Coba format:\n"
            "<code>/remind 30 menit lagi pesan kamu</code>\n"
            "<code>/remind 2 jam lagi pesan kamu</code>\n"
            "<code>/remind besok 09:00 pesan kamu</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    reminder_text = " ".join(args[2:]) or " ".join(args[1:])

    # Save to DB
    from datetime import timezone
    remind_at_str = remind_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    await db.save_reminder(db_path, user_id, chat_id, reminder_text, remind_at_str)

    # Try APScheduler
    scheduler: ReminderScheduler | None = context.application.bot_data.get("reminder_scheduler")
    scheduled = False
    if scheduler:
        scheduled = await scheduler.add_reminder(user_id, chat_id, reminder_text, remind_dt)

    local_time = remind_dt.strftime("%d %b %Y %H:%M UTC")
    await update.message.reply_text(
        f"✅ Pengingat diset!\n"
        f"⏰ Waktu: <b>{local_time}</b>\n"
        f"📝 Pesan: <i>{ui.escape(reminder_text[:200])}</i>"
        + ("\n\n⚠️ <i>APScheduler tidak tersedia — instal: pip install apscheduler</i>" if not scheduled else ""),
        parse_mode=ParseMode.HTML,
    )


# --- #6 /share ---------------------------------------------------------------

async def share_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Share current session as HTML: /share"""
    assert update.message is not None
    assert update.effective_user is not None

    user_id = update.effective_user.id
    db_path: str = context.application.bot_data["db_path"]

    sid = context.user_data.get("active_session_id") if context.user_data else None
    args = context.args or []
    if args and args[0].isdigit():
        sid = int(args[0])

    if not isinstance(sid, int):
        await update.message.reply_text(
            "❌ Tidak ada sesi aktif. Mulai chat dulu atau gunakan /history.",
        )
        return

    session = await db.get_session(db_path, sid)
    if session is None or session.user_id != user_id:
        await update.message.reply_text("❌ Sesi tidak ditemukan.")
        return

    html_bytes = await generate_session_html(db_path, sid)
    if html_bytes is None:
        await update.message.reply_text("❌ Gagal membuat halaman share.")
        return

    title = session.title or f"session_{sid}"
    safe_title = "".join(c if c.isalnum() or c in " -_" else "_" for c in title)[:40]
    filename = f"{safe_title}.html"

    import io
    await update.message.reply_document(
        document=io.BytesIO(html_bytes),
        filename=filename,
        caption=f"🌐 <b>{ui.escape(title)}</b>\n<i>{session.message_count} pesan — {session.model}</i>",
        parse_mode=ParseMode.HTML,
    )


# --- #44 /cost ---------------------------------------------------------------

async def cost_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show estimated cost for this session or overall."""
    assert update.message is not None
    assert update.effective_user is not None

    user_id = update.effective_user.id
    db_path: str = context.application.bot_data["db_path"]
    default_model = context.application.bot_data["default_model"]
    prefs = await _ensure_prefs(db_path, user_id, default_model)

    data = await db.user_analytics(db_path, user_id)
    total_cost = 0.0
    for model_name, _ in data["top_models"]:
        total_cost += cost_usd(model_name, data["tokens_in"] // 2, data["tokens_out"] // 2)

    tier = getattr(prefs, "tier", "free")
    await update.message.reply_text(
        f"💰 <b>Estimasi Biaya</b>\n"
        f"Tier: <code>{tier}</code>\n\n"
        f"Token masuk: <b>{data['tokens_in']:,}</b>\n"
        f"Token keluar: <b>{data['tokens_out']:,}</b>\n"
        f"Total token: <b>{data['tokens_in'] + data['tokens_out']:,}</b>\n\n"
        f"💵 Estimasi: <b>{format_cost(total_cost)}</b>\n"
        f"<i>(Estimasi kasar berdasarkan harga publik model)</i>",
        parse_mode=ParseMode.HTML,
    )


# --- #45 /settier (admin) ----------------------------------------------------

async def set_tier_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin: set user tier. /settier <user_id> <free|premium|admin>"""
    assert update.message is not None
    assert update.effective_user is not None

    if not _is_admin(context, update.effective_user.id):
        await update.message.reply_text("🚫 Hanya admin.")
        return

    args = context.args or []
    if len(args) < 2:
        await update.message.reply_text(
            "Format: <code>/settier &lt;user_id&gt; &lt;free|premium|admin&gt;</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    try:
        target_id = int(args[0])
    except ValueError:
        await update.message.reply_text("❌ User ID tidak valid.")
        return

    tier = args[1].lower()
    if tier not in ("free", "premium", "admin"):
        await update.message.reply_text("❌ Tier tidak valid. Pilih: free, premium, admin")
        return

    db_path: str = context.application.bot_data["db_path"]
    await db.upsert_user_prefs(db_path, target_id, tier=tier)
    await db.log_audit(
        db_path,
        admin_id=update.effective_user.id,
        action="settier",
        target_id=target_id,
        detail=f"tier set to {tier}",
    )
    await update.message.reply_text(
        f"✅ User <code>{target_id}</code> tier diset ke <b>{tier}</b>.",
        parse_mode=ParseMode.HTML,
    )


# --- #31 Photo / Document handler (OCR placeholder) --------------------------

async def photo_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle photo messages — describe or OCR the image."""
    assert update.message is not None
    assert update.effective_user is not None

    prefs, deny = await _admit(update, context)
    if prefs is None or deny:
        if deny:
            await update.message.reply_text(deny, parse_mode=ParseMode.HTML)
        return

    caption = update.message.caption or ""
    prompt_question = caption.strip() if caption.strip() else (
        "Deskripsikan gambar ini dalam bahasa Indonesia secara detail."
        if prefs.ui_language == "id" else
        "Describe this image in detail."
    )

    placeholder = await update.message.reply_text("🖼️ Memproses gambar...")

    # Try OCR if pytesseract is available
    ocr_text = ""
    try:
        import pytesseract
        from PIL import Image
        import io as _io

        photo = update.message.photo[-1]  # largest photo
        file = await context.bot.get_file(photo.file_id)
        file_bytes = await file.download_as_bytearray()
        img = Image.open(_io.BytesIO(bytes(file_bytes)))
        ocr_text = pytesseract.image_to_string(img, lang="ind+eng").strip()
    except Exception:
        pass

    db_path: str = context.application.bot_data["db_path"]
    default_model: str = context.application.bot_data["default_model"]
    user_id = update.effective_user.id
    model = _active_model(prefs, default_model)
    client: TansAIClient = context.application.bot_data["tans_client"]

    if ocr_text:
        prompt = f"Teks dari gambar (OCR):\n{ocr_text}\n\n{prompt_question}"
    else:
        prompt = (
            f"[Pengguna mengirim gambar.]\n"
            f"Caption/pertanyaan: {prompt_question}\n"
            f"(OCR tidak tersedia — jawab berdasarkan pertanyaan pengguna.)"
        )

    try:
        reply = await client.chat(message=prompt, model=model)
    except TansAIError as exc:
        await placeholder.edit_text(f"❌ {exc}")
        return

    from markdown_utils import to_telegram_html
    try:
        await placeholder.edit_text(
            to_telegram_html(reply), parse_mode=ParseMode.HTML
        )
    except Exception:
        await placeholder.edit_text(reply)


async def document_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle document/file uploads — extract text if possible."""
    assert update.message is not None
    assert update.effective_user is not None

    prefs, deny = await _admit(update, context)
    if prefs is None or deny:
        if deny:
            await update.message.reply_text(deny, parse_mode=ParseMode.HTML)
        return

    doc = update.message.document
    if doc is None:
        return

    caption = (update.message.caption or "").strip()
    placeholder = await update.message.reply_text(f"📄 Memproses dokumen: {ui.escape(doc.file_name or 'file')}...")

    # Only process text-like files
    text_mimes = {"text/plain", "text/csv", "application/json", "text/markdown", "text/html"}
    if doc.mime_type not in text_mimes:
        await placeholder.edit_text(
            f"⚠️ Tipe file <code>{ui.escape(doc.mime_type or 'unknown')}</code> belum didukung.\n"
            "Format yang didukung: .txt, .csv, .json, .md, .html",
            parse_mode=ParseMode.HTML,
        )
        return

    try:
        file = await context.bot.get_file(doc.file_id)
        content_bytes = await file.download_as_bytearray()
        content = content_bytes.decode("utf-8", errors="replace")[:4000]
    except Exception as exc:
        await placeholder.edit_text(f"❌ Gagal membaca file: {exc}")
        return

    question = caption or (
        "Analisis dan rangkum isi dokumen ini."
        if prefs.ui_language == "id" else
        "Analyze and summarize this document."
    )

    db_path: str = context.application.bot_data["db_path"]
    default_model: str = context.application.bot_data["default_model"]
    model = _active_model(prefs, default_model)
    client: TansAIClient = context.application.bot_data["tans_client"]

    # (#32) RAG ingestion if premium
    rag_msg = ""
    if getattr(prefs, "tier", "free") in ("premium", "admin") and _rag_store is not None:
        try:
            chunks = await _rag_store.ingest(update.effective_user.id, doc.file_name or "file", bytes(content_bytes))
            if chunks > 0:
                rag_msg = f"\n<i>(Dokumen disimpan ke memori AI: {chunks} chunks)</i>"
        except Exception as exc:
            logger.warning("RAG ingest failed: %s", exc)

    prompt = f"Dokumen: {doc.file_name}\n\nIsi:\n{content}\n\nPertanyaan: {question}"

    try:
        reply = await client.chat(message=prompt, model=model)
    except TansAIError as exc:
        await placeholder.edit_text(f"❌ {exc}")
        return

    from markdown_utils import to_telegram_html
    try:
        await placeholder.edit_text(
            f"📄 <b>{ui.escape(doc.file_name or 'Dokumen')}</b>\n\n{to_telegram_html(reply)}{rag_msg}",
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        await placeholder.edit_text(reply + rag_msg)


# ============================================================================
# v2 Tier-L Command Handlers
# ============================================================================

# --- #32 /rag ----------------------------------------------------------------

async def rag_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Manage RAG documents: /rag list | /rag delete <name>"""
    assert update.message is not None
    assert update.effective_user is not None

    prefs, deny = await _admit(update, context)
    if prefs is None or deny:
        if deny:
            await update.message.reply_text(deny, parse_mode=ParseMode.HTML)
        return

    if getattr(prefs, "tier", "free") not in ("premium", "admin"):
        await update.message.reply_text("💎 Fitur RAG (dokumen memori) hanya untuk pengguna Premium.")
        return

    user_id = update.effective_user.id
    args = context.args or []
    if not args:
        await update.message.reply_text(
            "📚 <b>RAG (Document Memory)</b>\n"
            "Format:\n"
            "<code>/rag list</code> — lihat dokumen tersimpan\n"
            "<code>/rag delete &lt;nama_file&gt;</code> — hapus dokumen\n\n"
            "💡 <i>Kirim file dokumen (PDF/TXT/DOCX) langsung untuk menyimpannya ke memori AI.</i>",
            parse_mode=ParseMode.HTML,
        )
        return

    cmd = args[0].lower()
    if cmd == "list":
        if _rag_store is None:
            await update.message.reply_text("❌ RAG store tidak aktif.")
            return
        docs = await _rag_store.list_documents(user_id)
        if not docs:
            await update.message.reply_text("📚 Belum ada dokumen yang disimpan.")
            return
        lines = ["📚 <b>Dokumen Tersimpan:</b>\n"]
        for d in docs:
            lines.append(f"• <code>{ui.escape(d)}</code>")
        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)

    elif cmd == "delete" and len(args) > 1:
        doc_name = " ".join(args[1:])
        if _rag_store is None:
            await update.message.reply_text("❌ RAG store tidak aktif.")
            return
        count = await _rag_store.delete_document(user_id, doc_name)
        if count:
            await update.message.reply_text(f"✅ Dihapus: <code>{ui.escape(doc_name)}</code> ({count} chunks)", parse_mode=ParseMode.HTML)
        else:
            await update.message.reply_text(f"❌ Dokumen <code>{ui.escape(doc_name)}</code> tidak ditemukan.", parse_mode=ParseMode.HTML)


# --- #37 /python -------------------------------------------------------------

async def python_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Execute Python code in a sandbox."""
    assert update.message is not None
    assert update.effective_user is not None

    prefs, deny = await _admit(update, context)
    if prefs is None or deny:
        if deny:
            await update.message.reply_text(deny, parse_mode=ParseMode.HTML)
        return

    # Check tier (premium/admin only to prevent abuse)
    tier = getattr(prefs, "tier", "free")
    if tier not in ("premium", "admin"):
        await update.message.reply_text("💎 Fitur Code Sandbox hanya untuk pengguna Premium.")
        return

    args = context.args or []
    code = update.message.text.split(maxsplit=1)[1] if len(args) else ""
    if not code.strip():
        await update.message.reply_text(
            "🐍 <b>Python Sandbox</b>\n"
            "Format: <code>/python print('hello')</code>\n\n"
            "💡 <i>Bisa multiline jika mengirim via newline. Kode dibatasi 10 detik, memori maks 64MB (Linux), dan no-network.</i>",
            parse_mode=ParseMode.HTML,
        )
        return

    # Strip markdown code blocks if present
    code = code.strip()
    if code.startswith("```"):
        code = code.split("\n", 1)[-1]
        if code.endswith("```"):
            code = code[:-3]
    
    placeholder = await update.message.reply_text("⚙️ Menjalankan kode...")
    result = await execute_code(code)
    out_text = format_sandbox_result(result)
    
    try:
        await placeholder.edit_text(out_text, parse_mode=ParseMode.HTML)
    except Exception:
        await placeholder.edit_text(result.output[:4000] if result.output else "Error formatting result.")


# --- #47 /premium (Telegram Stars) -------------------------------------------

async def premium_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show premium upgrade options using Telegram Stars."""
    assert update.message is not None
    assert update.effective_user is not None

    user_id = update.effective_user.id
    db_path: str = context.application.bot_data["db_path"]
    default_model: str = context.application.bot_data["default_model"]
    prefs = await _ensure_prefs(db_path, user_id, default_model)

    tier = getattr(prefs, "tier", "free")
    if tier == "admin":
        await update.message.reply_text("👑 Kamu adalah Admin (mendapatkan semua fitur premium otomatis).")
        return

    text = (
        "💎 <b>Tans AI Premium</b>\n\n"
        "Upgrade ke Premium untuk membuka semua fitur canggih:\n"
        "• 🤖 <b>Akses model terbaik</b> (tanpa batas fallback)\n"
        "• 📚 <b>RAG Document Memory</b> (ingatan dari file PDF/TXT/DOCX)\n"
        "• 🐍 <b>Python Sandbox</b> (eksekusi kode /python)\n"
        "• 💬 <b>Tanpa limit pesan harian</b>\n\n"
        f"<i>Status kamu saat ini: <b>{tier.upper()}</b></i>\n\n"
        "Pilih paket langganan menggunakan <b>Telegram Stars (XTR)</b>:"
    )

    kb = payment.premium_keyboard()
    if kb:
        await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
    else:
        await update.message.reply_text(text + "\n\n(Menu pembayaran tidak tersedia saat ini)", parse_mode=ParseMode.HTML)


async def buy_premium_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the 'buy' callback buttons for Telegram Stars."""
    query = update.callback_query
    if query is None:
        return

    await query.answer()
    payload = query.data.replace("buy_", "")  # premium_monthly | premium_once
    # Send invoice
    await payment.send_premium_invoice(update, context, payload)


if __name__ == "__main__":
    main()
