"""UI building blocks: keyboard labels, callback prefixes, and builders.

Centralised here so handlers in ``bot.py`` stay focused on logic and so the
labels can be reused across the codebase.
"""
from __future__ import annotations

import html
import re
from dataclasses import dataclass

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup

import db
import personas
import quick_prompts


# --- Reply keyboard labels -------------------------------------------------

BTN_NEW_CHAT = "\U0001f4dd New Chat"
BTN_HISTORY = "\U0001f4da History"
BTN_MODELS = "\U0001f9e0 Model"
BTN_QUICK = "\u26a1 Quick Prompt"
BTN_PERSONA = "\U0001f3ad Persona"
BTN_STATUS = "\U0001f50c Status"
BTN_HELP = "\u2139\ufe0f Bantuan"
BTN_SETTINGS = "\u2699\ufe0f Settings"
BTN_RESET = "\U0001f9f9 Reset Chat"


REPLY_BUTTON_LABELS = (
    BTN_NEW_CHAT,
    BTN_HISTORY,
    BTN_MODELS,
    BTN_QUICK,
    BTN_PERSONA,
    BTN_STATUS,
    BTN_HELP,
    BTN_SETTINGS,
    BTN_RESET,
)


def main_reply_keyboard() -> ReplyKeyboardMarkup:
    """Persistent reply keyboard with quick-access action buttons."""
    rows = [
        [KeyboardButton(BTN_NEW_CHAT), KeyboardButton(BTN_HISTORY)],
        [KeyboardButton(BTN_QUICK), KeyboardButton(BTN_PERSONA)],
        [KeyboardButton(BTN_MODELS), KeyboardButton(BTN_STATUS)],
        [KeyboardButton(BTN_SETTINGS), KeyboardButton(BTN_HELP)],
        [KeyboardButton(BTN_RESET)],
    ]
    return ReplyKeyboardMarkup(
        rows,
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Ketik pesan ke AI, atau pilih tombol di bawah...",
    )


# --- Callback data prefixes -------------------------------------------------

MODEL_PREFIX = "setmodel:"
SESSION_PREFIX = "loadsess:"
SESSION_ACTION_PREFIX = "sessact:"  # rename/pin/delete actions
SESSION_PAGE_PREFIX = "sesspage:"
REGEN_PREFIX = "regen:"
PERSONA_PREFIX = "persona:"
PERSONA_SESSION_PREFIX = "spersona:"
QUICK_PREFIX = "quick:"
SETTINGS_PREFIX = "settings:"
ONBOARD_PREFIX = "onbd:"
LANG_PREFIX = "lang:"
EXPORT_PREFIX = "export:"
SEARCH_PREFIX = "search:"


# --- Inline-keyboard builders ----------------------------------------------

@dataclass(frozen=True)
class PageInfo:
    page: int
    limit: int
    total: int

    @property
    def has_prev(self) -> bool:
        return self.page > 0

    @property
    def has_next(self) -> bool:
        return (self.page + 1) * self.limit < self.total

    @property
    def pages(self) -> int:
        if self.limit <= 0:
            return 1
        return max(1, -(-self.total // self.limit))  # ceil division


def models_keyboard(models: list[str], active_model: str) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for name in models:
        label = f"\u2705 {name}" if name == active_model else name
        payload = f"{MODEL_PREFIX}{name}"
        if len(payload.encode("utf-8")) > 64:
            continue
        rows.append([InlineKeyboardButton(text=label, callback_data=payload)])
    return InlineKeyboardMarkup(rows)


def _format_session_button_label(session: db.Session) -> str:
    title = session.title or "(belum ada pesan)"
    if len(title) > 32:
        title = title[:31] + "\u2026"
    date = session.updated_at.split("T", 1)[0] if session.updated_at else ""
    pin = "\U0001f4cc " if session.pinned else ""
    arch = " \U0001f5c4\ufe0f" if session.archived else ""
    return f"{pin}{title}{arch} \u2022 {date}" if date else f"{pin}{title}{arch}"


def history_keyboard(
    sessions: list[db.Session],
    active_session_id: int | None,
    page_info: PageInfo,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for s in sessions:
        marker = "\u25b6\ufe0f " if s.id == active_session_id else ""
        label = marker + _format_session_button_label(s)
        if len(label) > 60:
            label = label[:59] + "\u2026"
        rows.append(
            [
                InlineKeyboardButton(
                    text=label, callback_data=f"{SESSION_PREFIX}{s.id}"
                ),
                InlineKeyboardButton(
                    text="\u2026", callback_data=f"{SESSION_ACTION_PREFIX}menu:{s.id}"
                ),
            ]
        )
    # Pagination + utility row.
    nav: list[InlineKeyboardButton] = []
    if page_info.has_prev:
        nav.append(
            InlineKeyboardButton(
                text="\u2b05\ufe0f Prev",
                callback_data=f"{SESSION_PAGE_PREFIX}{page_info.page - 1}",
            )
        )
    nav.append(
        InlineKeyboardButton(
            text=f"{page_info.page + 1}/{page_info.pages}",
            callback_data=f"{SESSION_PAGE_PREFIX}noop",
        )
    )
    if page_info.has_next:
        nav.append(
            InlineKeyboardButton(
                text="Next \u27a1\ufe0f",
                callback_data=f"{SESSION_PAGE_PREFIX}{page_info.page + 1}",
            )
        )
    rows.append(nav)
    rows.append(
        [
            InlineKeyboardButton(
                text="\U0001f50d Cari", callback_data=f"{SEARCH_PREFIX}prompt"
            )
        ]
    )
    return InlineKeyboardMarkup(rows)


def session_action_menu(session: db.Session) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    text=("\U0001f4cc Unpin" if session.pinned else "\U0001f4cc Pin"),
                    callback_data=f"{SESSION_ACTION_PREFIX}pin:{session.id}",
                ),
                InlineKeyboardButton(
                    text="\u270f\ufe0f Rename",
                    callback_data=f"{SESSION_ACTION_PREFIX}rename:{session.id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text=(
                        "\U0001f5c4\ufe0f Unarchive"
                        if session.archived
                        else "\U0001f5c4\ufe0f Archive"
                    ),
                    callback_data=f"{SESSION_ACTION_PREFIX}archive:{session.id}",
                ),
                InlineKeyboardButton(
                    text="\U0001f4e5 Export",
                    callback_data=f"{EXPORT_PREFIX}{session.id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="\U0001f5d1\ufe0f Hapus",
                    callback_data=f"{SESSION_ACTION_PREFIX}delete:{session.id}",
                ),
                InlineKeyboardButton(
                    text="\u2b05\ufe0f Kembali",
                    callback_data=f"{SESSION_ACTION_PREFIX}back:{session.id}",
                ),
            ],
        ]
    )


def confirm_delete_keyboard(session_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    text="\u2705 Ya, hapus",
                    callback_data=f"{SESSION_ACTION_PREFIX}delconfirm:{session_id}",
                ),
                InlineKeyboardButton(
                    text="\u274c Batal",
                    callback_data=f"{SESSION_ACTION_PREFIX}menu:{session_id}",
                ),
            ]
        ]
    )


def reply_actions_keyboard(session_id: int) -> InlineKeyboardMarkup:
    """Inline buttons that appear under each AI reply."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    text="\U0001f504 Regenerate",
                    callback_data=f"{REGEN_PREFIX}{session_id}",
                ),
                InlineKeyboardButton(
                    text="\U0001f4e5 Export",
                    callback_data=f"{EXPORT_PREFIX}{session_id}",
                ),
            ]
        ]
    )


def personas_keyboard(active_key: str, *, for_session: bool = False) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    prefix = PERSONA_SESSION_PREFIX if for_session else PERSONA_PREFIX
    for key, p in personas.PERSONAS.items():
        if key == "custom":
            continue
        label = f"{p.emoji} {p.label}"
        if key == active_key:
            label = "\u2705 " + label
        rows.append(
            [InlineKeyboardButton(text=label, callback_data=f"{prefix}{key}")]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text=("\u2705 " if active_key == "custom" else "")
                + f"{personas.PERSONAS['custom'].emoji} Custom prompt",
                callback_data=f"{prefix}custom",
            )
        ]
    )
    return InlineKeyboardMarkup(rows)


def quick_prompts_keyboard() -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    items = list(quick_prompts.QUICK_PROMPTS.values())
    # Two per row for compact layout.
    for i in range(0, len(items), 2):
        row = []
        for q in items[i : i + 2]:
            row.append(
                InlineKeyboardButton(
                    text=f"{q.emoji} {q.label}", callback_data=f"{QUICK_PREFIX}{q.key}"
                )
            )
        rows.append(row)
    return InlineKeyboardMarkup(rows)


def settings_keyboard(prefs: db.UserPrefs, active_model: str) -> InlineKeyboardMarkup:
    persona = personas.get(prefs.persona)
    lang_label = "Bahasa: Indonesia" if prefs.ui_language == "id" else "Language: English"
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    text=f"\U0001f9e0 Model: {active_model}",
                    callback_data=f"{SETTINGS_PREFIX}model",
                )
            ],
            [
                InlineKeyboardButton(
                    text=f"{persona.emoji} Persona: {persona.label}",
                    callback_data=f"{SETTINGS_PREFIX}persona",
                )
            ],
            [
                InlineKeyboardButton(
                    text=f"\U0001f310 {lang_label}",
                    callback_data=f"{SETTINGS_PREFIX}lang",
                )
            ],
            [
                InlineKeyboardButton(
                    text="\U0001f4ca Statistik kamu",
                    callback_data=f"{SETTINGS_PREFIX}stats",
                )
            ],
            [
                InlineKeyboardButton(
                    text="\u270f\ufe0f Edit custom prompt",
                    callback_data=f"{SETTINGS_PREFIX}custom_prompt",
                )
            ],
        ]
    )


def language_keyboard(active: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    text=("\u2705 " if active == "id" else "") + "\U0001f1ee\U0001f1e9 Indonesia",
                    callback_data=f"{LANG_PREFIX}id",
                ),
                InlineKeyboardButton(
                    text=("\u2705 " if active == "en" else "") + "\U0001f1ec\U0001f1e7 English",
                    callback_data=f"{LANG_PREFIX}en",
                ),
            ]
        ]
    )


def onboarding_step1_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    text="\U0001f1ee\U0001f1e9 Indonesia", callback_data=f"{ONBOARD_PREFIX}lang:id"
                ),
                InlineKeyboardButton(
                    text="\U0001f1ec\U0001f1e7 English", callback_data=f"{ONBOARD_PREFIX}lang:en"
                ),
            ]
        ]
    )


def onboarding_persona_keyboard() -> InlineKeyboardMarkup:
    rows = []
    for key, p in personas.PERSONAS.items():
        if key == "custom":
            continue
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{p.emoji} {p.label} \u2014 {p.description}",
                    callback_data=f"{ONBOARD_PREFIX}persona:{key}",
                )
            ]
        )
    return InlineKeyboardMarkup(rows)


# --- Pure HTML helpers ------------------------------------------------------

def escape(text: str) -> str:
    return html.escape(text, quote=False)


_BUTTON_RE = re.compile(
    "|".join(re.escape(label) for label in REPLY_BUTTON_LABELS) + "$"
)


def is_button_label(text: str | None) -> bool:
    return text is not None and bool(_BUTTON_RE.match(text))
