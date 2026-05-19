"""Group chat & per-group context support for tansaibot (#41 #42 #43).

Provides:
  - Group session management (separate context per group)
  - Group admin permission checks (#43)
  - Rate-limit per group (#27)

Group sessions use user_id = -abs(chat_id) to keep them separate from
private user sessions while reusing the same DB schema.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# How we encode a group chat_id as a "user_id" in the DB
# (Telegram group IDs are already negative; we just store them directly)

def group_user_id(chat_id: int) -> int:
    """Return a synthetic user_id for group session storage."""
    # Groups have negative IDs in Telegram; store as-is.
    return chat_id


def is_group_chat(chat_id: int) -> bool:
    return chat_id < 0


def build_group_prompt_prefix(sender_name: str) -> str:
    """Add speaker attribution for group conversations."""
    return f"[{sender_name}]: "
