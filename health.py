"""Lightweight HTTP health & metrics server for tansaibot (#15).

Runs a tiny aiohttp server on port 8081 (configurable via env HEALTH_PORT).

Endpoints:
  GET /healthz  — liveness probe
                  200 {"status":"ok","db":"ok","tans_ai":"ok"}
                  503 if any component is unhealthy

  GET /metricsz — Prometheus-compatible metrics
                  requests_total, errors_total, p50/p95/p99 latency, etc.

Usage (in bot.py):
    from health import HealthServer
    health = HealthServer(db_path, tans_client)
    await health.start()   # non-blocking
    ...
    await health.stop()    # on shutdown
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections import deque
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tans_client import TansAIClient

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Metrics store (in-memory, single-process)
# ---------------------------------------------------------------------------

class MetricsStore:
    """Accumulates bot metrics thread-safely."""

    def __init__(self) -> None:
        self.requests_total: int = 0
        self.errors_total: int = 0
        # Rolling window of latencies (seconds) — last 1000 requests
        self._latencies: deque[float] = deque(maxlen=1000)
        self._lock = asyncio.Lock()

    async def record(self, latency_s: float, error: bool = False) -> None:
        async with self._lock:
            self.requests_total += 1
            if error:
                self.errors_total += 1
            self._latencies.append(latency_s)

    async def percentile(self, p: float) -> float:
        async with self._lock:
            if not self._latencies:
                return 0.0
            sorted_lat = sorted(self._latencies)
            idx = max(0, int(len(sorted_lat) * p / 100) - 1)
            return sorted_lat[idx]

    async def snapshot(self) -> dict:
        p50 = await self.percentile(50)
        p95 = await self.percentile(95)
        p99 = await self.percentile(99)
        async with self._lock:
            return {
                "requests_total": self.requests_total,
                "errors_total": self.errors_total,
                "latency_p50_ms": round(p50 * 1000, 1),
                "latency_p95_ms": round(p95 * 1000, 1),
                "latency_p99_ms": round(p99 * 1000, 1),
            }


# Singleton — import and use from anywhere
metrics = MetricsStore()


# ---------------------------------------------------------------------------
# Health server
# ---------------------------------------------------------------------------

class HealthServer:
    """Minimal async HTTP server using raw asyncio (no extra deps)."""

    def __init__(
        self,
        db_path: str | Path,
        tans_client: "TansAIClient | None" = None,
        port: int | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.tans_client = tans_client
        self.port = port or int(os.getenv("HEALTH_PORT", "8081"))
        self._server: asyncio.Server | None = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._handle, "0.0.0.0", self.port
        )
        logger.info("Health server listening on port %d", self.port)
        asyncio.create_task(self._server.serve_forever())

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            logger.info("Health server stopped")

    async def _handle(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            raw = await asyncio.wait_for(reader.read(1024), timeout=5.0)
            request_line = raw.decode(errors="ignore").split("\r\n")[0]
            path = request_line.split(" ")[1] if " " in request_line else "/"

            if path.startswith("/healthz"):
                body, status = await self._healthz()
            elif path.startswith("/metricsz"):
                body, status = await self._metricsz()
            else:
                body, status = {"error": "not found"}, 404

            body_bytes = json.dumps(body, ensure_ascii=False).encode()
            response = (
                f"HTTP/1.1 {status} {'OK' if status == 200 else 'ERROR'}\r\n"
                f"Content-Type: application/json\r\n"
                f"Content-Length: {len(body_bytes)}\r\n"
                f"Connection: close\r\n\r\n"
            ).encode() + body_bytes

            writer.write(response)
            await writer.drain()
        except Exception:
            pass
        finally:
            writer.close()

    async def _healthz(self) -> tuple[dict, int]:
        checks: dict[str, str] = {}

        # DB check
        try:
            import sqlite3
            conn = sqlite3.connect(str(self.db_path))
            conn.execute("SELECT 1").fetchone()
            conn.close()
            checks["db"] = "ok"
        except Exception as exc:
            checks["db"] = f"error: {exc}"

        # Tans AI check (circuit breaker state)
        if self.tans_client is not None:
            state = self.tans_client.circuit_state
            checks["tans_ai"] = "ok" if state == "closed" else f"degraded ({state})"
        else:
            checks["tans_ai"] = "unknown"

        healthy = all(v == "ok" for v in checks.values())
        status = 200 if healthy else 503
        return {"status": "ok" if healthy else "degraded", **checks}, status

    async def _metricsz(self) -> tuple[dict, int]:
        snap = await metrics.snapshot()
        return snap, 200
