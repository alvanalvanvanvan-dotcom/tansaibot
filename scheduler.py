"""Scheduled reminders for tansaibot (#40).

Uses APScheduler for persistence-free in-memory scheduling.
Reminders survive bot crashes IF APScheduler is configured with a SQLite
job store (optional — set SCHEDULER_DB_PATH env var).

Usage:
    from scheduler import ReminderScheduler
    scheduler = ReminderScheduler(bot)
    await scheduler.start()
    await scheduler.add_reminder(user_id, chat_id, "backup laptop", when_dt)
    await scheduler.stop()
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

logger = logging.getLogger(__name__)

try:
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.date import DateTrigger
    _APScheduler_AVAILABLE = True
except ImportError:
    _APScheduler_AVAILABLE = False
    logger.warning("APScheduler not installed — /remind disabled. pip install apscheduler")

if TYPE_CHECKING:
    from telegram import Bot


class ReminderScheduler:
    """Wraps APScheduler to send Telegram reminders at a specific datetime."""

    def __init__(self, bot: "Bot") -> None:
        self.bot = bot
        self._scheduler: "AsyncIOScheduler | None" = None
        self._available = _APScheduler_AVAILABLE

    async def start(self) -> None:
        if not self._available:
            return
        from apscheduler.schedulers.asyncio import AsyncIOScheduler

        db_path = os.getenv("SCHEDULER_DB_PATH", "")
        jobstores = {}
        if db_path:
            from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
            jobstores["default"] = SQLAlchemyJobStore(url=f"sqlite:///{db_path}")

        self._scheduler = AsyncIOScheduler(
            jobstores=jobstores or None,
            timezone="UTC",
        )
        self._scheduler.start()
        logger.info("Reminder scheduler started (APScheduler)")

    async def stop(self) -> None:
        if self._scheduler:
            self._scheduler.shutdown(wait=False)
            logger.info("Reminder scheduler stopped")

    async def add_reminder(
        self,
        user_id: int,
        chat_id: int,
        text: str,
        when: datetime,
    ) -> bool:
        """Schedule a reminder. Returns True if scheduled, False if APScheduler unavailable."""
        if not self._available or self._scheduler is None:
            return False

        from apscheduler.triggers.date import DateTrigger

        # Ensure UTC
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)

        job_id = f"remind_{user_id}_{when.timestamp():.0f}"

        self._scheduler.add_job(
            self._send_reminder,
            trigger=DateTrigger(run_date=when),
            args=[chat_id, user_id, text],
            id=job_id,
            replace_existing=True,
            misfire_grace_time=300,
        )
        logger.info("Scheduled reminder for user %d at %s", user_id, when.isoformat())
        return True

    async def _send_reminder(self, chat_id: int, user_id: int, text: str) -> None:
        try:
            await self.bot.send_message(
                chat_id=chat_id,
                text=f"⏰ <b>Pengingat!</b>\n\n{text}\n\n<i>(Dikirim oleh tansaibot scheduler)</i>",
                parse_mode="HTML",
            )
        except Exception as exc:
            logger.error("Failed to send reminder to %d: %s", user_id, exc)


# ---------------------------------------------------------------------------
# Natural language time parser (minimal, no extra deps)
# ---------------------------------------------------------------------------

_TZ_OFFSET = int(os.getenv("TZ_OFFSET_HOURS", "7"))  # WIB default


def parse_reminder_time(text: str) -> datetime | None:
    """Parse common Indonesian/English time expressions.

    Examples:
      "besok 10 pagi"        → tomorrow 10:00 WIB
      "30 menit lagi"        → now + 30 minutes
      "2 jam lagi"           → now + 2 hours
      "Selasa 14:00"         → next Tuesday 14:00
      "20 Mei 08:30"         → 2026-05-20 08:30 local
    """
    import re

    now_local = datetime.now(tz=timezone(timedelta(hours=_TZ_OFFSET)))
    text = text.lower().strip()

    # "X menit/jam lagi"
    m = re.search(r"(\d+)\s*(menit|minute|jam|hour)", text)
    if m:
        val = int(m.group(1))
        unit = m.group(2)
        if "menit" in unit or "minute" in unit:
            dt = now_local + timedelta(minutes=val)
        else:
            dt = now_local + timedelta(hours=val)
        return dt.astimezone(timezone.utc)

    # "besok HH:MM" or "besok jam H pagi/sore"
    is_tomorrow = "besok" in text or "tomorrow" in text
    m = re.search(r"(\d{1,2})(?::(\d{2}))?\s*(pagi|siang|sore|malam|am|pm)?", text)
    if m:
        hour = int(m.group(1))
        minute = int(m.group(2) or 0)
        period = m.group(3) or ""
        if period in ("sore", "malam", "pm") and hour < 12:
            hour += 12
        if period == "pagi" and hour == 12:
            hour = 0
        base = now_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if is_tomorrow:
            base = base + timedelta(days=1)
        elif base <= now_local:
            base = base + timedelta(days=1)
        return base.astimezone(timezone.utc)

    return None
