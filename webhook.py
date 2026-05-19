"""Webhook mode support for tansaibot (#21).

When WEBHOOK_URL is set in the environment, the bot runs in webhook mode
instead of long polling. Webhook mode has ~3-5x lower latency and is
more production-appropriate.

The webhook server is started automatically by main() in bot.py when
WEBHOOK_URL is detected.

Required env vars for webhook mode:
    WEBHOOK_URL        — public HTTPS URL (e.g. https://yourdomain.com/bot)
    WEBHOOK_PORT       — local port to listen on (default: 8443)
    WEBHOOK_SECRET     — secret token for Telegram to validate requests
    WEBHOOK_CERT_PATH  — path to SSL cert (only needed for self-signed)
    WEBHOOK_KEY_PATH   — path to SSL key (only needed for self-signed)

Example nginx config:
    location /bot {
        proxy_pass http://localhost:8443/;
    }
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)


def get_webhook_config() -> dict | None:
    """Return webhook config dict, or None if webhook mode is disabled."""
    url = os.getenv("WEBHOOK_URL", "").strip()
    if not url:
        return None

    return {
        "url": url,
        "port": int(os.getenv("WEBHOOK_PORT", "8443")),
        "secret_token": os.getenv("WEBHOOK_SECRET", ""),
        "cert": os.getenv("WEBHOOK_CERT_PATH", "") or None,
        "key": os.getenv("WEBHOOK_KEY_PATH", "") or None,
        "listen": "0.0.0.0",
        "path": f"/{url.rstrip('/').split('/')[-1]}",  # last path component
    }


def is_webhook_mode() -> bool:
    return bool(os.getenv("WEBHOOK_URL", "").strip())
