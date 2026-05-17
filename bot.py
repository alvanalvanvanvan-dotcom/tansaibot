"""Telegram bot that proxies messages to a Tans AI API Gateway."""
from __future__ import annotations

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
    KeyboardButton,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.constants import ChatAction, ParseMode
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
from tans_client import TansAIClient, TansAIError

logger = logging.getLogger(__name__)

# Per-user model preference (in-memory: lost on restart, no need to persist).
USER_MODEL: dict[int, str] = {}
# Per-user pointer to the currently active session id in the DB.
USER_ACTIVE_SESSION: dict[int, int] = {}


def _config() -> dict[str, str | int | float]:
    load_dotenv()
    required = ["TELEGRAM_BOT_TOKEN", "TANS_AI_API_KEY"]
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        raise SystemExit(
            "Environment variable wajib belum di-set: " + ", ".join(missing)
        )
    default_db_path = str(Path(__file__).resolve().parent / "chat_history.db")
    return {
        "telegram_token": os.environ["TELEGRAM_BOT_TOKEN"],
        "tans_base_url": os.getenv("TANS_AI_BASE_URL", "http://localhost:20130/api/v1"),
        "tans_api_key": os.environ["TANS_AI_API_KEY"],
        "default_model": os.getenv("TANS_AI_DEFAULT_MODEL", "gpt-4o"),
        "history_max": int(os.getenv("HISTORY_MAX_MESSAGES", "20")),
        "timeout": float(os.getenv("TANS_AI_TIMEOUT", "60")),
        "db_path": os.getenv("CHAT_DB_PATH", default_db_path),
    }


def _model_for(user_id: int, default: str) -> str:
    return USER_MODEL.get(user_id, default)


# --- Reply keyboard ---------------------------------------------------------

BTN_NEW_CHAT = "\U0001f4dd New Chat"
BTN_HISTORY = "\U0001f4da History"
BTN_MODELS = "\U0001f9e0 Pilih Model"
BTN_STATUS = "\U0001f50c Status"
BTN_HELP = "\u2139\ufe0f Bantuan"
BTN_RESET = "\U0001f9f9 Reset Chat"


def _main_reply_keyboard() -> ReplyKeyboardMarkup:
    """Persistent reply keyboard with quick-access command buttons."""
    rows = [
        [KeyboardButton(BTN_NEW_CHAT), KeyboardButton(BTN_HISTORY)],
        [KeyboardButton(BTN_MODELS), KeyboardButton(BTN_STATUS)],
        [KeyboardButton(BTN_HELP), KeyboardButton(BTN_RESET)],
    ]
    return ReplyKeyboardMarkup(
        rows,
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Ketik pesan ke AI, atau pilih tombol di bawah...",
    )


# --- Session helpers --------------------------------------------------------

MAX_TITLE_LEN = 40


def _make_title(first_message: str) -> str:
    title = " ".join(first_message.split())
    if len(title) > MAX_TITLE_LEN:
        title = title[: MAX_TITLE_LEN - 1].rstrip() + "\u2026"
    return title or "(tanpa judul)"


def _format_session_button_label(session: db.Session) -> str:
    title = session.title or "(belum ada pesan)"
    if len(title) > 32:
        title = title[:31] + "\u2026"
    # updated_at is ISO 8601 UTC; show date only to keep button short.
    date = session.updated_at.split("T", 1)[0] if session.updated_at else ""
    return f"{title} \u2022 {date}" if date else title


async def _get_or_create_active_session(
    db_path: str, user_id: int, model: str
) -> int:
    """Return the user's active session id, creating one on first use."""
    sid = USER_ACTIVE_SESSION.get(user_id)
    if sid is not None:
        session = await db.get_session(db_path, sid)
        if session is not None and session.user_id == user_id:
            return sid
        # stale pointer — fall through and create a new one
    new_sid = await db.create_session(db_path, user_id=user_id, model=model)
    USER_ACTIVE_SESSION[user_id] = new_sid
    return new_sid


def _build_prompt(history: list[db.Message], new_message: str) -> str:
    """Compose a single prompt string from prior turns plus the new user message.

    The Tans AI /chat endpoint only takes a single `message` string, so we
    serialize the recent conversation in a readable format.
    """
    if not history:
        return new_message
    lines: list[str] = []
    for msg in history:
        prefix = "User" if msg.role == "user" else "Assistant"
        lines.append(f"{prefix}: {msg.content}")
    lines.append(f"User: {new_message}")
    lines.append("Assistant:")
    return "\n".join(lines)


# --- Command handlers -------------------------------------------------------

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    assert update.effective_user is not None
    assert update.message is not None
    default_model: str = context.application.bot_data["default_model"]
    model = _model_for(update.effective_user.id, default_model)
    await update.message.reply_text(
        "Halo! Saya bot AI yang terhubung ke Tans AI.\n\n"
        f"Model aktif: <code>{html.escape(model)}</code>\n\n"
        "Tap salah satu tombol di bawah, atau ketik pesan apa saja untuk chat.",
        parse_mode=ParseMode.HTML,
        reply_markup=_main_reply_keyboard(),
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    assert update.message is not None
    text = (
        "<b>Cara pakai bot:</b>\n"
        "Gunakan tombol di bawah area ketik, atau ketik command:\n\n"
        f"\u2022 <b>{BTN_NEW_CHAT}</b> / /new - mulai percakapan baru kosong\n"
        f"\u2022 <b>{BTN_HISTORY}</b> / /history - lihat riwayat percakapan\n"
        f"\u2022 <b>{BTN_MODELS}</b> / /models - pilih model AI lewat tombol\n"
        f"\u2022 <b>{BTN_STATUS}</b> / /status - cek koneksi ke Tans AI\n"
        f"\u2022 <b>{BTN_HELP}</b> / /help - bantuan ini\n"
        f"\u2022 <b>{BTN_RESET}</b> / /reset - hapus percakapan aktif sekarang\n\n"
        "/start - tampilkan ulang tombol\n"
        "/model &lt;nama&gt; - ganti model manual via teks\n\n"
        "Untuk chat, langsung ketik pesan apa saja. Bot mengingat seluruh "
        "percakapan di sesi aktif (tersimpan permanen ke SQLite)."
    )
    await update.message.reply_text(
        text, parse_mode=ParseMode.HTML, reply_markup=_main_reply_keyboard()
    )


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    assert update.message is not None
    client: TansAIClient = context.application.bot_data["tans_client"]
    try:
        info = await client.status()
    except TansAIError as exc:
        await update.message.reply_text(
            f"<b>Status: GAGAL</b>\n<code>{html.escape(str(exc))}</code>",
            parse_mode=ParseMode.HTML,
        )
        return
    pretty = json.dumps(info, indent=2, ensure_ascii=False)
    if len(pretty) > 3500:
        pretty = pretty[:3500] + "\n... (truncated)"
    await update.message.reply_text(
        f"<b>Status: OK</b>\n<pre>{html.escape(pretty)}</pre>",
        parse_mode=ParseMode.HTML,
    )


# --- Models (inline keyboard) -----------------------------------------------

MODEL_CALLBACK_PREFIX = "setmodel:"


def _build_models_keyboard(models: list[str], active_model: str) -> InlineKeyboardMarkup:
    """Build a 1-column inline keyboard with one button per model."""
    rows: list[list[InlineKeyboardButton]] = []
    for name in models:
        label = f"\u2705 {name}" if name == active_model else name
        payload = f"{MODEL_CALLBACK_PREFIX}{name}"
        if len(payload.encode("utf-8")) > 64:
            continue
        rows.append([InlineKeyboardButton(text=label, callback_data=payload)])
    return InlineKeyboardMarkup(rows)


async def models_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    assert update.message is not None
    assert update.effective_user is not None
    client: TansAIClient = context.application.bot_data["tans_client"]
    try:
        models = await client.list_models()
    except TansAIError as exc:
        await update.message.reply_text(f"Gagal mengambil daftar model: {exc}")
        return
    if not models:
        await update.message.reply_text("Tidak ada model yang dikembalikan oleh API.")
        return

    default_model: str = context.application.bot_data["default_model"]
    active = _model_for(update.effective_user.id, default_model)
    keyboard = _build_models_keyboard(models, active)
    if not keyboard.inline_keyboard:
        formatted = "\n".join(f"- <code>{html.escape(m)}</code>" for m in models)
        await update.message.reply_text(
            f"<b>Model tersedia:</b>\n{formatted}\n\n"
            "Pakai <code>/model &lt;nama&gt;</code> untuk mengganti.",
            parse_mode=ParseMode.HTML,
        )
        return

    await update.message.reply_text(
        f"<b>Pilih model AI</b> (aktif: <code>{html.escape(active)}</code>):\n"
        "Tap salah satu di bawah, atau ketik <code>/model &lt;nama&gt;</code>.",
        reply_markup=keyboard,
        parse_mode=ParseMode.HTML,
    )


async def model_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or query.data is None or update.effective_user is None:
        return
    if not query.data.startswith(MODEL_CALLBACK_PREFIX):
        await query.answer()
        return

    new_model = query.data[len(MODEL_CALLBACK_PREFIX):]
    if not new_model:
        await query.answer("Model tidak valid.", show_alert=True)
        return

    user_id = update.effective_user.id
    USER_MODEL[user_id] = new_model
    await query.answer(f"Model diganti ke {new_model}")

    client: TansAIClient = context.application.bot_data["tans_client"]
    try:
        models = await client.list_models()
    except TansAIError:
        models = []

    if models and query.message is not None:
        keyboard = _build_models_keyboard(models, new_model)
        try:
            await query.edit_message_text(
                f"<b>Pilih model AI</b> (aktif: <code>{html.escape(new_model)}</code>):\n"
                "Tap salah satu di bawah, atau ketik <code>/model &lt;nama&gt;</code>.",
                reply_markup=keyboard,
                parse_mode=ParseMode.HTML,
            )
            return
        except Exception:  # noqa: BLE001 - editing may fail if message is too old
            logger.debug("Failed to edit /models message", exc_info=True)

    if query.message is not None:
        await query.message.reply_text(
            f"Model diganti ke <code>{html.escape(new_model)}</code>.",
            parse_mode=ParseMode.HTML,
        )


async def model_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    assert update.message is not None
    assert update.effective_user is not None
    args = context.args or []
    default_model: str = context.application.bot_data["default_model"]
    if not args:
        current = _model_for(update.effective_user.id, default_model)
        await update.message.reply_text(
            f"Model aktif kamu: <code>{html.escape(current)}</code>\n"
            "Pakai <code>/model &lt;nama&gt;</code> untuk mengganti, atau /models untuk daftar.",
            parse_mode=ParseMode.HTML,
        )
        return
    new_model = args[0].strip()
    if not new_model:
        await update.message.reply_text("Nama model tidak boleh kosong.")
        return
    USER_MODEL[update.effective_user.id] = new_model
    await update.message.reply_text(
        f"Model diganti ke <code>{html.escape(new_model)}</code>.",
        parse_mode=ParseMode.HTML,
    )


# --- New Chat ---------------------------------------------------------------

async def new_chat_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    assert update.message is not None
    assert update.effective_user is not None
    user_id = update.effective_user.id
    db_path: str = context.application.bot_data["db_path"]
    default_model: str = context.application.bot_data["default_model"]
    model = _model_for(user_id, default_model)

    new_sid = await db.create_session(db_path, user_id=user_id, model=model)
    USER_ACTIVE_SESSION[user_id] = new_sid
    await update.message.reply_text(
        "\U0001f195 <b>Percakapan baru dimulai</b>\n"
        f"Model aktif: <code>{html.escape(model)}</code>\n\n"
        "Kirim pesan apa saja untuk memulai.",
        parse_mode=ParseMode.HTML,
        reply_markup=_main_reply_keyboard(),
    )


# --- History ----------------------------------------------------------------

SESSION_CALLBACK_PREFIX = "loadsession:"
HISTORY_LIMIT = 20


def _build_history_keyboard(
    sessions: list[db.Session], active_session_id: int | None
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for s in sessions:
        prefix = "\u25b6\ufe0f " if s.id == active_session_id else ""
        label = prefix + _format_session_button_label(s)
        if len(label) > 60:
            label = label[:59] + "\u2026"
        payload = f"{SESSION_CALLBACK_PREFIX}{s.id}"
        rows.append([InlineKeyboardButton(text=label, callback_data=payload)])
    return InlineKeyboardMarkup(rows)


async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    assert update.message is not None
    assert update.effective_user is not None
    user_id = update.effective_user.id
    db_path: str = context.application.bot_data["db_path"]
    sessions = await db.list_sessions(db_path, user_id=user_id, limit=HISTORY_LIMIT)
    if not sessions:
        await update.message.reply_text(
            "Belum ada riwayat percakapan. Tap <b>New Chat</b> atau langsung "
            "kirim pesan untuk memulai.",
            parse_mode=ParseMode.HTML,
        )
        return

    active = USER_ACTIVE_SESSION.get(user_id)
    keyboard = _build_history_keyboard(sessions, active)
    await update.message.reply_text(
        f"<b>Riwayat percakapan</b> ({len(sessions)} sesi terakhir)\n"
        "Tap salah satu untuk melanjutkan.\n"
        "Tanda \u25b6\ufe0f = sesi aktif sekarang.",
        reply_markup=keyboard,
        parse_mode=ParseMode.HTML,
    )


async def load_session_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    if query is None or query.data is None or update.effective_user is None:
        return
    if not query.data.startswith(SESSION_CALLBACK_PREFIX):
        await query.answer()
        return

    try:
        sid = int(query.data[len(SESSION_CALLBACK_PREFIX):])
    except ValueError:
        await query.answer("Session ID tidak valid.", show_alert=True)
        return

    db_path: str = context.application.bot_data["db_path"]
    session = await db.get_session(db_path, sid)
    if session is None or session.user_id != update.effective_user.id:
        await query.answer("Sesi tidak ditemukan.", show_alert=True)
        return

    user_id = update.effective_user.id
    USER_ACTIVE_SESSION[user_id] = sid
    # Keep model preference in sync with the session's model so subsequent
    # messages use the model the session was created with.
    USER_MODEL[user_id] = session.model

    await query.answer("Sesi dimuat.")

    # Refresh the keyboard so the active marker moves.
    sessions = await db.list_sessions(db_path, user_id=user_id, limit=HISTORY_LIMIT)
    keyboard = _build_history_keyboard(sessions, sid)

    title = session.title or "(belum ada pesan)"
    last_messages = await db.get_messages(db_path, sid, limit=2)
    preview_lines: list[str] = []
    for m in last_messages:
        role = "Kamu" if m.role == "user" else "AI"
        content = m.content if len(m.content) <= 200 else m.content[:200] + "\u2026"
        preview_lines.append(f"<b>{role}:</b> {html.escape(content)}")
    preview = "\n".join(preview_lines) if preview_lines else "(belum ada pesan di sesi ini)"

    text = (
        f"\u25b6\ufe0f <b>Melanjutkan:</b> {html.escape(title)}\n"
        f"Model: <code>{html.escape(session.model)}</code>\n"
        f"Pesan: {session.message_count}\n\n"
        f"{preview}\n\n"
        "Ketik pesan untuk melanjutkan."
    )

    if query.message is not None:
        try:
            await query.edit_message_text(
                text, reply_markup=keyboard, parse_mode=ParseMode.HTML
            )
            return
        except Exception:  # noqa: BLE001 - editing may fail; fall back to new message
            logger.debug("Failed to edit history message", exc_info=True)
        await query.message.reply_text(text, parse_mode=ParseMode.HTML)


# --- Reset ------------------------------------------------------------------

async def reset_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    assert update.message is not None
    assert update.effective_user is not None
    user_id = update.effective_user.id
    db_path: str = context.application.bot_data["db_path"]
    sid = USER_ACTIVE_SESSION.pop(user_id, None)
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


# --- Chat message -----------------------------------------------------------

async def chat_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or update.effective_user is None:
        return
    user_message = (update.message.text or "").strip()
    if not user_message:
        return

    user_id = update.effective_user.id
    client: TansAIClient = context.application.bot_data["tans_client"]
    default_model: str = context.application.bot_data["default_model"]
    history_max: int = context.application.bot_data["history_max"]
    db_path: str = context.application.bot_data["db_path"]
    model = _model_for(user_id, default_model)

    await context.bot.send_chat_action(
        chat_id=update.effective_chat.id if update.effective_chat else user_id,
        action=ChatAction.TYPING,
    )

    sid = await _get_or_create_active_session(db_path, user_id, model)
    history = await db.get_messages(db_path, sid, limit=history_max)
    prompt = _build_prompt(history, user_message)

    try:
        reply = await client.chat(message=prompt, model=model)
    except TansAIError as exc:
        await update.message.reply_text(f"Gagal memanggil Tans AI: {exc}")
        return
    except Exception:  # noqa: BLE001 - surface unexpected errors to the user
        logger.exception("Unexpected error while calling Tans AI")
        await update.message.reply_text(
            "Terjadi kesalahan tak terduga saat memanggil Tans AI. "
            "Cek log bot untuk detail."
        )
        return

    reply = reply.strip() or "(AI mengembalikan response kosong.)"

    # Persist both sides of the turn.
    await db.add_message(db_path, sid, role="user", content=user_message)
    await db.add_message(db_path, sid, role="assistant", content=reply)

    # Set the title from the first user message if not set yet.
    if not history:
        try:
            await db.update_session_title(db_path, sid, _make_title(user_message))
        except Exception:  # noqa: BLE001 - title is non-critical
            logger.debug("Failed to set session title", exc_info=True)

    await update.message.reply_text(reply)


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

    application.add_handler(
        CallbackQueryHandler(model_callback, pattern=f"^{MODEL_CALLBACK_PREFIX}")
    )
    application.add_handler(
        CallbackQueryHandler(load_session_callback, pattern=f"^{SESSION_CALLBACK_PREFIX}")
    )

    # Reply-keyboard button taps arrive as plain text. Route each label to the
    # matching command handler BEFORE the catch-all chat handler.
    button_routes = [
        (BTN_NEW_CHAT, new_chat_command),
        (BTN_HISTORY, history_command),
        (BTN_MODELS, models_command),
        (BTN_STATUS, status_command),
        (BTN_HELP, help_command),
        (BTN_RESET, reset_command),
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
