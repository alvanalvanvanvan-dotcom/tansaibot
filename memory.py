"""Persistent user memory across sessions (#35).

Users can say /remember <fact> to store long-term facts about themselves.
These are injected as a "User Context" block in every system prompt.

Schema stored in user_preferences.long_term_memory as JSON text.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

MAX_MEMORIES = 20  # cap per user


def _load(raw: str) -> list[str]:
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            return [str(x) for x in data]
    except Exception:
        pass
    return []


def _dump(memories: list[str]) -> str:
    return json.dumps(memories, ensure_ascii=False)


def build_memory_prompt(raw: str) -> str:
    """Format stored memories as a system-prompt block."""
    memories = _load(raw)
    if not memories:
        return ""
    lines = "\n".join(f"- {m}" for m in memories)
    return f"[User Context — ingat ini selalu]\n{lines}"


def add_memory(raw: str, fact: str) -> tuple[str, bool]:
    """Add a fact. Returns (new_raw, was_duplicate)."""
    memories = _load(raw)
    fact = fact.strip()
    if not fact:
        return raw, False
    if fact.lower() in (m.lower() for m in memories):
        return raw, True
    memories.append(fact)
    if len(memories) > MAX_MEMORIES:
        memories = memories[-MAX_MEMORIES:]
    return _dump(memories), False


def remove_memory(raw: str, index: int) -> tuple[str, bool]:
    """Remove memory by 1-based index. Returns (new_raw, success)."""
    memories = _load(raw)
    if index < 1 or index > len(memories):
        return raw, False
    memories.pop(index - 1)
    return _dump(memories), True


def list_memories(raw: str) -> list[str]:
    return _load(raw)


def clear_memories() -> str:
    return _dump([])
