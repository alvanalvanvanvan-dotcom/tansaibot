"""Structured JSON logging setup for tansaibot (#13).

Usage:
    from logging_setup import setup_logging
    setup_logging()  # call once at startup in bot.py

All log records will include:
  - timestamp (ISO-8601 UTC)
  - level
  - logger name
  - message
  - Any extra fields passed as keyword args to logger.info/warning/etc.

If structlog is not installed, falls back to standard Python logging
with a compact text format so the bot never crashes on import.
"""
from __future__ import annotations

import logging
import sys

try:
    import structlog

    _STRUCTLOG_AVAILABLE = True
except ImportError:
    _STRUCTLOG_AVAILABLE = False


def setup_logging(level: str = "INFO") -> None:
    """Configure root logger.

    Args:
        level: Logging level string, e.g. "INFO", "DEBUG", "WARNING".
    """
    numeric_level = getattr(logging, level.upper(), logging.INFO)

    if _STRUCTLOG_AVAILABLE:
        structlog.configure(
            processors=[
                structlog.stdlib.add_log_level,
                structlog.stdlib.add_logger_name,
                structlog.processors.TimeStamper(fmt="iso", utc=True),
                structlog.processors.StackInfoRenderer(),
                structlog.processors.format_exc_info,
                structlog.processors.JSONRenderer(),
            ],
            wrapper_class=structlog.make_filtering_bound_logger(numeric_level),
            context_class=dict,
            logger_factory=structlog.PrintLoggerFactory(sys.stdout),
            cache_logger_on_first_use=True,
        )
        # Redirect stdlib logging through structlog
        logging.basicConfig(
            format="%(message)s",
            stream=sys.stdout,
            level=numeric_level,
        )
        logging.getLogger().setLevel(numeric_level)
    else:
        # Fallback: clean plain-text format
        logging.basicConfig(
            format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%SZ",
            stream=sys.stdout,
            level=numeric_level,
        )

    # Silence noisy third-party loggers
    for noisy in ("httpx", "httpcore", "telegram", "apscheduler"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
