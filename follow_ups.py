"""Smart follow-up suggestions.

After each AI reply we ask the model for 3 short follow-up questions the user
might want to ask next, and surface them as inline buttons under the reply.

Keep this lightweight: 3 plain strings, length-capped for Telegram callback
data and button labels. The actual scheduling lives in ``bot.py``; this
module is pure logic + parsing so it stays unit-testable.
"""
from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tans_client import TansAIClient

logger = logging.getLogger(__name__)


MAX_SUGGESTIONS = 3
MAX_LABEL_LEN = 48  # Telegram inline button text limit is ~64 bytes


def _prompt_id(last_user: str, last_assistant: str) -> str:
    return (
        "Berdasarkan pertukaran percakapan di bawah, sarankan tiga pertanyaan "
        "lanjutan SINGKAT (max 8 kata) yang mungkin ingin diajukan pengguna "
        "berikutnya. Jawab HANYA dengan JSON array berisi 3 string, "
        "tanpa penjelasan, tanpa pembungkus markdown.\n\n"
        f"User: {last_user}\nAssistant: {last_assistant}\n\n"
        'Contoh format jawaban: ["...", "...", "..."]'
    )


def _prompt_en(last_user: str, last_assistant: str) -> str:
    return (
        "Given the conversation exchange below, suggest three SHORT follow-up "
        "questions (max 8 words) the user might want to ask next. "
        "Reply ONLY with a JSON array of exactly 3 strings, no explanation, "
        "no markdown wrappers.\n\n"
        f"User: {last_user}\nAssistant: {last_assistant}\n\n"
        'Example format: ["...", "...", "..."]'
    )


def _truncate(s: str, n: int = MAX_LABEL_LEN) -> str:
    s = s.strip().strip("\"'").strip()
    if len(s) > n:
        s = s[: n - 1].rstrip() + "\u2026"
    return s


_JSON_ARRAY_RE = re.compile(r"\[\s*(?:\".*?\"|'.*?'|[^\[\]])*\]", re.DOTALL)


def parse_suggestions(raw: str) -> list[str]:
    """Extract up to 3 suggestion strings from the model's reply."""
    if not raw:
        return []
    raw = raw.strip()
    candidates: list[str] = []
    # 1) Look for the first JSON-array-like substring.
    match = _JSON_ARRAY_RE.search(raw)
    if match is not None:
        try:
            data = json.loads(match.group(0))
            if isinstance(data, list):
                candidates = [str(x) for x in data if isinstance(x, (str, int))]
        except json.JSONDecodeError:
            candidates = []
    # 2) Fallback: split on newlines / bullets if JSON parse failed.
    if not candidates:
        for line in raw.splitlines():
            cleaned = re.sub(r"^[\s\-\d\.\)\u2022]+", "", line).strip()
            if cleaned:
                candidates.append(cleaned)
    # Truncate and de-dup, keep order.
    seen: set[str] = set()
    out: list[str] = []
    for c in candidates:
        t = _truncate(c)
        if t and t not in seen:
            seen.add(t)
            out.append(t)
        if len(out) >= MAX_SUGGESTIONS:
            break
    return out


async def generate(
    client: "TansAIClient",
    *,
    model: str,
    last_user_message: str,
    last_assistant_message: str,
    language: str = "id",
) -> list[str]:
    """Ask the model for 3 short follow-up questions.

    Returns ``[]`` on any error (best-effort; never raises).
    """
    if not last_user_message and not last_assistant_message:
        return []
    last_user = last_user_message[:600]
    last_assistant = last_assistant_message[:1200]
    prompt = (
        _prompt_en(last_user, last_assistant)
        if language == "en"
        else _prompt_id(last_user, last_assistant)
    )
    try:
        raw = await client.chat(message=prompt, model=model)
    except Exception:  # noqa: BLE001
        logger.debug("follow-up generation failed", exc_info=True)
        return []
    return parse_suggestions(raw or "")
