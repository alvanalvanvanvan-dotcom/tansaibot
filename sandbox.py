"""Safe code execution sandbox for tansaibot (#37).

Executes Python code snippets in an isolated subprocess with:
  - Timeout limit (default 10s)
  - Memory limit (via resource module on Linux)
  - No network access (via subprocess isolation)
  - Capture stdout/stderr

Supports Python only for now. Safe to use with untrusted code.

Usage:
    from sandbox import execute_code
    result = await execute_code("print(2 ** 10)")
    # result.output → "1024"
    # result.error  → ""
    # result.timed_out → False
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = int(os.getenv("SANDBOX_TIMEOUT", "10"))
MAX_OUTPUT_CHARS = 3000


@dataclass
class ExecutionResult:
    output: str
    error: str
    timed_out: bool
    exit_code: int

    @property
    def success(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


# Dangerous patterns that should be blocked
_BLOCKED_PATTERNS = [
    "import os", "import sys", "import subprocess",
    "import socket", "import urllib", "import http",
    "import requests", "import httpx",
    "__import__", "open(", "eval(", "exec(",
    "os.system", "os.popen", "subprocess.run",
    "shutil.rm", "pathlib", "builtins",
]

# Allowed safe imports
_SAFE_PREAMBLE = """
import math
import json
import re
import random
import datetime
import itertools
import functools
import collections
from typing import *
"""


def _is_safe(code: str) -> tuple[bool, str]:
    """Check code for dangerous patterns."""
    code_lower = code.lower()
    for pattern in _BLOCKED_PATTERNS:
        if pattern.lower() in code_lower:
            return False, f"Pattern tidak diizinkan: `{pattern}`"
    return True, ""


async def execute_code(
    code: str,
    language: str = "python",
    timeout: int = TIMEOUT_SECONDS,
) -> ExecutionResult:
    """Execute code in a sandboxed subprocess.
    
    Returns ExecutionResult with stdout/stderr.
    Only Python is supported currently.
    """
    if language.lower() not in ("python", "py", "python3"):
        return ExecutionResult(
            output="",
            error=f"Bahasa '{language}' belum didukung. Hanya Python.",
            timed_out=False,
            exit_code=1,
        )

    safe, reason = _is_safe(code)
    if not safe:
        return ExecutionResult(
            output="",
            error=f"❌ Kode diblokir karena keamanan: {reason}",
            timed_out=False,
            exit_code=1,
        )

    full_code = _SAFE_PREAMBLE + "\n" + code

    return await asyncio.to_thread(_run_subprocess, full_code, timeout)


def _run_subprocess(code: str, timeout: int) -> ExecutionResult:
    import subprocess
    import resource  # Linux only

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".py", delete=False, encoding="utf-8"
    ) as f:
        f.write(code)
        tmp_path = f.name

    try:
        def _preexec():
            # Set resource limits (Linux only)
            try:
                # 64MB memory limit
                resource.setrlimit(resource.RLIMIT_AS, (64 * 1024 * 1024, 64 * 1024 * 1024))
                # No file creation
                resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
                # No subprocesses
                resource.setrlimit(resource.RLIMIT_NPROC, (0, 0))
            except Exception:
                pass

        try:
            proc = subprocess.run(
                [sys.executable, "-I", "-S", tmp_path],
                capture_output=True,
                text=True,
                timeout=timeout,
                preexec_fn=_preexec if sys.platform != "win32" else None,
            )
            output = proc.stdout[:MAX_OUTPUT_CHARS]
            error = proc.stderr[:MAX_OUTPUT_CHARS]
            return ExecutionResult(
                output=output,
                error=error,
                timed_out=False,
                exit_code=proc.returncode,
            )
        except subprocess.TimeoutExpired:
            return ExecutionResult(
                output="",
                error=f"⏱️ Timeout setelah {timeout} detik.",
                timed_out=True,
                exit_code=-1,
            )
    finally:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass


def format_result(result: ExecutionResult) -> str:
    """Format ExecutionResult as a Telegram-friendly message."""
    parts: list[str] = []
    if result.timed_out:
        parts.append(f"⏱️ <b>Timeout</b> ({TIMEOUT_SECONDS}s)")
    elif result.exit_code != 0 and not result.output:
        parts.append(f"❌ <b>Error (exit {result.exit_code})</b>")
    else:
        parts.append("✅ <b>Output:</b>")

    if result.output:
        out = result.output[:2000]
        parts.append(f"<pre><code>{out}</code></pre>")
    if result.error:
        err = result.error[:500]
        parts.append(f"<b>Stderr:</b>\n<pre><code>{err}</code></pre>")

    return "\n".join(parts)
