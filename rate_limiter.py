"""Simple async per-user token bucket rate limiter.

Two windows are enforced independently:

- **per minute** — short burst control (default 30).
- **per day** — long-term abuse control (default 500).

Both windows are sliding fixed-size buckets stored in memory only.  This is
fine for a single-process bot; on bot restart, counters reset.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class RateLimitResult:
    allowed: bool
    retry_after: int = 0
    reason: str = ""  # "minute" or "day" when blocked


class RateLimiter:
    """In-memory per-user rate limiter.

    Use ``check_and_consume(user_id)`` before serving a request; if it
    returns ``allowed=False`` send the user a friendly throttling message
    using ``retry_after`` seconds.
    """

    def __init__(
        self,
        per_minute: int = 30,
        per_day: int = 500,
        admin_ids: tuple[int, ...] = (),
    ) -> None:
        self.per_minute = max(0, int(per_minute))
        self.per_day = max(0, int(per_day))
        self._admins = set(admin_ids)
        self._lock = asyncio.Lock()
        # user_id -> list of timestamps (seconds), unsorted but capped.
        self._minute: dict[int, list[float]] = {}
        self._day: dict[int, list[float]] = {}

    def is_disabled(self) -> bool:
        return self.per_minute <= 0 and self.per_day <= 0

    def _prune(self, bucket: list[float], window: float, now: float) -> None:
        cutoff = now - window
        # In-place filter
        bucket[:] = [t for t in bucket if t >= cutoff]

    async def check_and_consume(self, user_id: int) -> RateLimitResult:
        if user_id in self._admins:
            return RateLimitResult(True)
        async with self._lock:
            now = time.monotonic()
            minute = self._minute.setdefault(user_id, [])
            day = self._day.setdefault(user_id, [])
            self._prune(minute, 60.0, now)
            self._prune(day, 86400.0, now)
            if self.per_minute > 0 and len(minute) >= self.per_minute:
                oldest = min(minute)
                wait = max(1, int(60 - (now - oldest)) + 1)
                return RateLimitResult(False, retry_after=wait, reason="minute")
            if self.per_day > 0 and len(day) >= self.per_day:
                oldest = min(day)
                wait = max(60, int(86400 - (now - oldest)) + 1)
                return RateLimitResult(False, retry_after=wait, reason="day")
            minute.append(now)
            day.append(now)
            return RateLimitResult(True)

    async def remaining(self, user_id: int) -> tuple[int, int]:
        """Return ``(remaining_per_minute, remaining_per_day)``."""
        if user_id in self._admins:
            return (self.per_minute, self.per_day)
        async with self._lock:
            now = time.monotonic()
            minute = self._minute.setdefault(user_id, [])
            day = self._day.setdefault(user_id, [])
            self._prune(minute, 60.0, now)
            self._prune(day, 86400.0, now)
            rm = max(0, self.per_minute - len(minute)) if self.per_minute > 0 else -1
            rd = max(0, self.per_day - len(day)) if self.per_day > 0 else -1
            return (rm, rd)


def format_retry(seconds: int, language: str = "id") -> str:
    if language == "en":
        if seconds < 60:
            return f"{seconds}s"
        m, s = divmod(seconds, 60)
        if m < 60:
            return f"{m}m {s}s" if s else f"{m}m"
        h, m = divmod(m, 60)
        return f"{h}h {m}m" if m else f"{h}h"
    # Indonesian
    if seconds < 60:
        return f"{seconds} detik"
    m, s = divmod(seconds, 60)
    if m < 60:
        return f"{m} menit {s} detik" if s else f"{m} menit"
    h, m = divmod(m, 60)
    return f"{h} jam {m} menit" if m else f"{h} jam"
