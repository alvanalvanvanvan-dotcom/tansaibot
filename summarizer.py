"""Smart context summarization.

When a chat session grows past a threshold we replace the oldest messages
with a compact ``Summary so far: ...`` paragraph. The summary is persisted in
the ``sessions`` table together with the message id it was generated up to,
so we only re-summarize when ``SUMMARIZATION_DELTA`` new messages have piled
on top of the last snapshot.

Pure logic lives here; bot.py wires it into ``_send_ai_reply``.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import db
    from tans_client import TansAIClient

logger = logging.getLogger(__name__)


MAX_SUMMARY_LEN = 1200


def _instruction_id(message_count: int, prev_summary: str) -> str:
    prefix = "Berikut " + (
        "ringkasan percakapan SEBELUMNYA dan " if prev_summary else ""
    ) + f"{message_count} pesan terakhir yang BELUM diringkas. "
    return (
        prefix
        + "Buat ringkasan yang ringkas, faktual, dan netral dari SELURUH "
        "percakapan (gabungkan ringkasan lama jika ada dengan pesan baru), "
        "fokus pada konteks penting yang harus diingat AI: keputusan, "
        "preferensi user, fakta yang sudah dikonfirmasi, dan to-do.\n"
        "- Hindari basa-basi.\n"
        "- Maksimal sekitar 800 kata, gunakan bullet ringkas jika perlu.\n"
        "- Jangan tambahkan kalimat penutup.\n"
        "- Jangan ulangi instruksi ini.\n"
    )


def _instruction_en(message_count: int, prev_summary: str) -> str:
    prefix = "Below is " + (
        "the PREVIOUS conversation summary and " if prev_summary else ""
    ) + f"the most recent {message_count} messages NOT yet summarized. "
    return (
        prefix
        + "Produce a tight, factual, neutral summary of the WHOLE "
        "conversation (merge the old summary, if any, with the new messages) "
        "that captures everything an AI must remember: decisions, user "
        "preferences, confirmed facts, and to-dos.\n"
        "- No filler.\n"
        "- ~800 words max, use short bullets if helpful.\n"
        "- No closing sentence.\n"
        "- Do not repeat these instructions.\n"
    )


def build_summary_prompt(
    messages: list["db.Message"], previous_summary: str, language: str
) -> str:
    """Build the prompt fed into the chat endpoint for summary generation."""
    instr = (
        _instruction_en(len(messages), previous_summary)
        if language == "en"
        else _instruction_id(len(messages), previous_summary)
    )
    lines: list[str] = [instr.strip(), ""]
    if previous_summary:
        lines.append("Previous summary:")
        lines.append(previous_summary.strip())
        lines.append("")
    lines.append("New messages:")
    for m in messages:
        role = "User" if m.role == "user" else "Assistant"
        lines.append(f"{role}: {m.content}")
    lines.append("")
    lines.append("Summary:")
    return "\n".join(lines)


def should_summarize(
    *,
    total_messages: int,
    summarized_count: int,
    threshold: int,
    keep_last: int,
    delta: int,
) -> bool:
    """Return True if it's time to (re)generate the summary.

    ``total_messages``  total number of messages persisted in the session.
    ``summarized_count`` how many were already covered by the existing summary.
    ``threshold`` minimum total before summarization kicks in at all.
    ``keep_last`` how many newest messages we always pass verbatim.
    ``delta`` how many new messages can accumulate before we re-summarize.
    """
    if threshold <= 0:
        return False
    if total_messages < threshold:
        return False
    new_to_cover = max(0, total_messages - keep_last - summarized_count)
    return new_to_cover >= max(1, delta)


def messages_to_summarize(
    all_messages: list["db.Message"],
    *,
    summarized_count: int,
    keep_last: int,
) -> list["db.Message"]:
    end = max(0, len(all_messages) - keep_last)
    return all_messages[summarized_count:end]


async def generate_summary(
    client: "TansAIClient",
    *,
    model: str,
    messages: list["db.Message"],
    previous_summary: str,
    language: str,
) -> str:
    """Best-effort summary generation. Returns empty string on failure."""
    if not messages:
        return previous_summary
    prompt = build_summary_prompt(messages, previous_summary, language)
    try:
        raw = await client.chat(message=prompt, model=model)
    except Exception:  # noqa: BLE001
        logger.debug("summary generation failed", exc_info=True)
        return previous_summary
    text = (raw or "").strip()
    if len(text) > MAX_SUMMARY_LEN:
        text = text[: MAX_SUMMARY_LEN - 1].rstrip() + "\u2026"
    return text
