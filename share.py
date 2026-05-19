"""Share session as read-only HTML page (#6).

Generates a self-contained HTML file from a chat session that can be
served statically or sent as a Telegram document.

Usage:
    from share import generate_session_html
    html_bytes = await generate_session_html(db_path, session_id)
"""
from __future__ import annotations

import asyncio
import html as html_lib
from pathlib import Path
from typing import TYPE_CHECKING

import db


_CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
       background: #0d1117; color: #e6edf3; min-height: 100vh; padding: 2rem 1rem; }
.container { max-width: 800px; margin: 0 auto; }
h1 { font-size: 1.4rem; color: #58a6ff; margin-bottom: 0.5rem; }
.meta { font-size: 0.85rem; color: #8b949e; margin-bottom: 2rem; }
.message { margin-bottom: 1.2rem; display: flex; gap: 0.75rem; }
.message.user { flex-direction: row-reverse; }
.bubble { padding: 0.75rem 1rem; border-radius: 12px; max-width: 80%;
          line-height: 1.6; white-space: pre-wrap; word-break: break-word; }
.user .bubble { background: #1f6feb; color: #fff; border-radius: 12px 12px 0 12px; }
.assistant .bubble { background: #161b22; border: 1px solid #30363d;
                     border-radius: 12px 12px 12px 0; }
.label { font-size: 0.75rem; color: #8b949e; margin-bottom: 0.25rem;
         text-align: right; }
.user .label { text-align: left; }
code { background: #0d1117; padding: 0.1em 0.4em; border-radius: 4px;
       font-family: monospace; font-size: 0.9em; }
pre { background: #0d1117; padding: 1rem; border-radius: 8px; overflow-x: auto; }
footer { margin-top: 3rem; text-align: center; color: #484f58; font-size: 0.8rem; }
"""


async def generate_session_html(db_path: str, session_id: int) -> bytes | None:
    """Generate a self-contained HTML export of a session.

    Returns bytes of the HTML file, or None if session not found.
    """
    session = await db.get_session(db_path, session_id)
    if session is None:
        return None

    messages = await db.get_all_messages(db_path, session_id)

    title = html_lib.escape(session.title or f"Session #{session_id}")
    model = html_lib.escape(session.model)
    created = session.created_at[:10]
    msg_count = len(messages)

    rows: list[str] = []
    for msg in messages:
        role_class = "user" if msg.role == "user" else "assistant"
        label = "Kamu" if msg.role == "user" else f"AI ({model})"
        content = html_lib.escape(msg.content)
        rows.append(
            f'<div class="message {role_class}">'
            f'<div>'
            f'<div class="label">{label}</div>'
            f'<div class="bubble">{content}</div>'
            f'</div>'
            f'</div>'
        )

    body_html = "\n".join(rows)

    page = f"""<!DOCTYPE html>
<html lang="id">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} — tansaibot</title>
<style>{_CSS}</style>
</head>
<body>
<div class="container">
  <h1>{title}</h1>
  <div class="meta">Model: {model} &nbsp;·&nbsp; {created} &nbsp;·&nbsp; {msg_count} pesan</div>
  {body_html}
  <footer>Diekspor dari tansaibot &nbsp;·&nbsp; tansai.dev</footer>
</div>
</body>
</html>"""

    return page.encode("utf-8")
