"""Helpers for a "typing animation" placeholder while we wait on the API.

The Tans AI ``/chat`` endpoint returns the full reply in one shot, so true
token-by-token streaming isn't possible. To still give the bot a modern
"thinking" feel we send a placeholder message and rotate its text every
~1.2s until the reply arrives.
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from telegram.error import BadRequest, RetryAfter, TimedOut

if TYPE_CHECKING:  # pragma: no cover
    from telegram import Message

logger = logging.getLogger(__name__)


FRAMES_ID = (
    "\u23f3 Memikirkan jawaban\u2026",
    "\u23f3 Memikirkan jawaban\u2026 .",
    "\u23f3 Memikirkan jawaban\u2026 . .",
    "\u23f3 Memikirkan jawaban\u2026 . . .",
)

FRAMES_EN = (
    "\u23f3 Thinking\u2026",
    "\u23f3 Thinking\u2026 .",
    "\u23f3 Thinking\u2026 . .",
    "\u23f3 Thinking\u2026 . . .",
)


def frames_for(language: str) -> tuple[str, ...]:
    return FRAMES_EN if language == "en" else FRAMES_ID


async def animate(message: "Message", stop_event: asyncio.Event, language: str = "id") -> None:
    """Rotate ``message`` through animation frames until ``stop_event`` is set."""
    frames = frames_for(language)
    i = 1  # frame 0 was already used when we sent the placeholder
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=1.2)
            return
        except asyncio.TimeoutError:
            pass
        try:
            await message.edit_text(frames[i % len(frames)])
        except (BadRequest, TimedOut):
            # Either the message was already edited or the network blipped;
            # neither is fatal — just keep looping.
            pass
        except RetryAfter as exc:
            await asyncio.sleep(float(exc.retry_after) + 0.5)
        except Exception:  # noqa: BLE001 - non-critical animation
            logger.debug("placeholder animation failed", exc_info=True)
        i += 1
