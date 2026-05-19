"""Real SSE streaming update for Tier M (#1).

Replaces the old placeholder-animation approach when the Tans AI gateway
supports `stream: true` (Server-Sent Events / chunked HTTP).

Usage:
    from streaming import animate, stream_to_message

    # Old way (placeholder animation, still used as fallback):
    stop = asyncio.Event()
    anim = asyncio.create_task(animate(placeholder_msg, stop))

    # New way (SSE streaming):
    final_text = await stream_to_message(
        client, placeholder_msg, message=user_text, model=model,
        language=prefs.ui_language,
    )
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, AsyncIterator

from telegram.error import BadRequest, RetryAfter, TimedOut

if TYPE_CHECKING:
    from telegram import Message
    from tans_client import TansAIClient

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
    """Rotate ``message`` through animation frames until ``stop_event`` is set.
    Used as fallback when SSE streaming is unavailable.
    """
    frames = frames_for(language)
    i = 1
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=1.2)
            return
        except asyncio.TimeoutError:
            pass
        try:
            await message.edit_text(frames[i % len(frames)])
        except (BadRequest, TimedOut):
            pass
        except RetryAfter as exc:
            await asyncio.sleep(float(exc.retry_after) + 0.5)
        except Exception:  # noqa: BLE001
            logger.debug("placeholder animation failed", exc_info=True)
        i += 1


# ---------------------------------------------------------------------------
# Real SSE streaming (#1)
# ---------------------------------------------------------------------------

# Minimum interval between Telegram edits (Telegram rate: ~1 edit/s per msg)
_EDIT_INTERVAL = 1.0


async def stream_to_message(
    client: "TansAIClient",
    placeholder: "Message",
    *,
    message: str,
    model: str,
    language: str = "id",
) -> str:
    """Stream AI response tokens into an existing Telegram message.

    Edits ``placeholder`` in-place every ~1s as chunks arrive.
    Returns the full accumulated text (whether streaming succeeded or not).

    Falls back to placeholder animation + single full reply if the endpoint
    does not emit SSE (detected by checking if any chunks arrive).
    """
    accumulated = ""
    last_edit_time = 0.0
    got_any_chunk = False
    stop_event = asyncio.Event()

    # Start animation as fallback (cancelled when first chunk arrives)
    anim_task = asyncio.create_task(animate(placeholder, stop_event, language))

    try:
        async for chunk in client.chat_stream(message=message, model=model):
            got_any_chunk = True
            # Cancel animation on first real chunk
            if not stop_event.is_set():
                stop_event.set()
                try:
                    await anim_task
                except Exception:  # noqa: BLE001
                    pass

            accumulated += chunk
            now = asyncio.get_event_loop().time()
            if now - last_edit_time >= _EDIT_INTERVAL:
                last_edit_time = now
                cursor = "▌" if accumulated else ""
                try:
                    await placeholder.edit_text(
                        accumulated + cursor,
                        # No parse_mode during streaming to avoid HTML errors
                        # mid-stream; final edit will apply HTML.
                    )
                except (BadRequest, TimedOut, RetryAfter):
                    pass
                except Exception:  # noqa: BLE001
                    logger.debug("stream edit failed", exc_info=True)

    except Exception as exc:  # noqa: BLE001
        # Streaming failed — caller will handle the error
        if not stop_event.is_set():
            stop_event.set()
        try:
            await anim_task
        except Exception:  # noqa: BLE001
            pass
        raise exc

    finally:
        stop_event.set()
        try:
            await anim_task
        except Exception:  # noqa: BLE001
            pass

    return accumulated
