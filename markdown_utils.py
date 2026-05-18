"""Convert AI markdown replies into Telegram-safe HTML.

Telegram's HTML mode supports a small set of tags
(https://core.telegram.org/bots/api#html-style):
``<b>``, ``<i>``, ``<u>``, ``<s>``, ``<code>``, ``<pre>``, ``<a>``, ``<blockquote>``.
This helper takes lightweight markdown (the format AI assistants typically
produce) and emits well-formed HTML for that subset.

The implementation is intentionally simple and forgiving: any input that
fails to parse falls back to plain-text escape. We never raise.
"""
from __future__ import annotations

import html
import re
from dataclasses import dataclass

# Telegram message limit is 4096 chars. Leave headroom for inline keyboards
# being rendered separately.
TELEGRAM_MESSAGE_LIMIT = 4000

_FENCE_RE = re.compile(r"```([a-zA-Z0-9_+\-]*)\n(.*?)```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_BOLD_RE = re.compile(r"\*\*([^\*\n]+)\*\*")
_ITALIC_RE = re.compile(r"(?<![\*_])\*([^\*\n]+)\*(?!\*)")
_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
_HEADER_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)
_BULLET_RE = re.compile(r"^[\-\*]\s+", re.MULTILINE)
_BLOCKQUOTE_RE = re.compile(r"(?:^>\s?.*(?:\n|$))+", re.MULTILINE)


@dataclass
class _Placeholder:
    token: str
    html: str


def _stash(parts: list[_Placeholder], html_fragment: str, index: int) -> str:
    token = f"\x00PLH{index}\x00"
    parts.append(_Placeholder(token=token, html=html_fragment))
    return token


def _convert_code_blocks(text: str, stash: list[_Placeholder]) -> str:
    def repl(match: re.Match[str]) -> str:
        lang = match.group(1).strip()
        body = match.group(2)
        body_escaped = html.escape(body, quote=False).rstrip("\n")
        if lang:
            fragment = (
                f"<pre><code class=\"language-{html.escape(lang, quote=True)}\">"
                f"{body_escaped}</code></pre>"
            )
        else:
            fragment = f"<pre>{body_escaped}</pre>"
        return _stash(stash, fragment, len(stash))

    return _FENCE_RE.sub(repl, text)


def _convert_inline_code(text: str, stash: list[_Placeholder]) -> str:
    def repl(match: re.Match[str]) -> str:
        body = html.escape(match.group(1), quote=False)
        return _stash(stash, f"<code>{body}</code>", len(stash))

    return _INLINE_CODE_RE.sub(repl, text)


def _convert_links(text: str, stash: list[_Placeholder]) -> str:
    def repl(match: re.Match[str]) -> str:
        label = html.escape(match.group(1), quote=False)
        href = html.escape(match.group(2), quote=True)
        return _stash(stash, f"<a href=\"{href}\">{label}</a>", len(stash))

    return _LINK_RE.sub(repl, text)


def _convert_blockquotes(text: str) -> str:
    def repl(match: re.Match[str]) -> str:
        block = match.group(0)
        lines = [re.sub(r"^>\s?", "", line) for line in block.split("\n") if line]
        inner = "\n".join(lines)
        return f"<blockquote>{inner}</blockquote>"

    return _BLOCKQUOTE_RE.sub(repl, text)


def _convert_headers(text: str) -> str:
    def repl(match: re.Match[str]) -> str:
        return f"<b>{match.group(2)}</b>"

    return _HEADER_RE.sub(repl, text)


def _convert_bullets(text: str) -> str:
    return _BULLET_RE.sub("\u2022 ", text)


def _restore(text: str, stash: list[_Placeholder]) -> str:
    for ph in stash:
        text = text.replace(ph.token, ph.html)
    return text


def to_telegram_html(text: str) -> str:
    """Convert markdown-ish AI output to Telegram HTML.

    The function never raises: on unexpected input it escapes everything
    as plain text.
    """
    if not text:
        return ""
    try:
        stash: list[_Placeholder] = []
        # Stash code blocks FIRST so their content is preserved verbatim.
        work = _convert_code_blocks(text, stash)
        work = _convert_inline_code(work, stash)
        work = _convert_links(work, stash)
        # Escape what remains.
        work = html.escape(work, quote=False)
        # Re-introduce simple formatting.
        work = _BOLD_RE.sub(r"<b>\1</b>", work)
        work = _ITALIC_RE.sub(r"<i>\1</i>", work)
        work = _convert_headers(work)
        work = _convert_bullets(work)
        work = _convert_blockquotes(work)
        work = _restore(work, stash)
        return work
    except Exception:  # noqa: BLE001 - last-resort safety net
        return html.escape(text, quote=False)


def chunk_for_telegram(text: str, limit: int = TELEGRAM_MESSAGE_LIMIT) -> list[str]:
    """Split a long text into chunks that fit in a Telegram message.

    Tries to split on paragraph boundaries first, then on newlines, then on
    word boundaries.
    """
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        # Prefer splitting at a double newline before the limit.
        split = remaining.rfind("\n\n", 0, limit)
        if split == -1 or split < limit // 2:
            split = remaining.rfind("\n", 0, limit)
        if split == -1 or split < limit // 2:
            split = remaining.rfind(" ", 0, limit)
        if split == -1:
            split = limit
        chunks.append(remaining[:split].rstrip())
        remaining = remaining[split:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks
