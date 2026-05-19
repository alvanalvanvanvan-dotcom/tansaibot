"""HTTP client for the Tans AI API Gateway.

v2 improvements:
- HTTP/2 + connection pooling (keepalive) for lower latency (#2)
- Smart retry with exponential backoff for transient errors (#3)
- Circuit breaker: fail-fast if Tans AI is consecutively down (#4)
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class TansAIError(Exception):
    """Raised when the Tans AI API returns an error or is unreachable."""


# ---------------------------------------------------------------------------
# Circuit Breaker (#4)
# ---------------------------------------------------------------------------

class CircuitBreaker:
    """Simple circuit breaker: open after N consecutive failures within window.

    States:
      CLOSED  — normal operation
      OPEN    — fail-fast (no requests) until recovery_seconds pass
      HALF    — one probe allowed; success closes, fail re-opens
    """

    CLOSED = "closed"
    OPEN = "open"
    HALF = "half_open"

    def __init__(
        self,
        fail_threshold: int = 3,
        recovery_seconds: float = 60.0,
    ) -> None:
        self.fail_threshold = fail_threshold
        self.recovery_seconds = recovery_seconds
        self._state = self.CLOSED
        self._failures = 0
        self._opened_at: float = 0.0
        self._lock = asyncio.Lock()

    @property
    def state(self) -> str:
        return self._state

    async def is_open(self) -> bool:
        async with self._lock:
            if self._state == self.OPEN:
                if time.monotonic() - self._opened_at >= self.recovery_seconds:
                    self._state = self.HALF
                    logger.info("Circuit breaker → HALF-OPEN (probe allowed)")
                    return False
                return True
            return False

    async def record_success(self) -> None:
        async with self._lock:
            self._failures = 0
            if self._state != self.CLOSED:
                logger.info("Circuit breaker → CLOSED")
            self._state = self.CLOSED

    async def record_failure(self) -> None:
        async with self._lock:
            self._failures += 1
            if self._state == self.HALF or self._failures >= self.fail_threshold:
                self._state = self.OPEN
                self._opened_at = time.monotonic()
                logger.warning(
                    "Circuit breaker → OPEN (failures=%d, recovery in %ds)",
                    self._failures,
                    int(self.recovery_seconds),
                )


# ---------------------------------------------------------------------------
# TansAI HTTP Client (#2 #3)
# ---------------------------------------------------------------------------

# Shared circuit breaker instance (module-level singleton)
_circuit_breaker = CircuitBreaker(fail_threshold=3, recovery_seconds=60.0)


class TansAIClient:
    """Async HTTP client for Tans AI API Gateway.

    Features:
    - HTTP/2 with persistent keepalive connection pool
    - Automatic retry with exponential backoff on transient errors
    - Circuit breaker integration
    """

    # Retry config
    _MAX_RETRIES = 3
    _RETRY_BASE_DELAY = 1.0  # seconds; doubles each attempt

    # Transient HTTP status codes worth retrying
    _RETRYABLE_STATUS = {429, 500, 502, 503, 504}

    def __init__(self, base_url: str, api_key: str, timeout: float = 60.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self._client: httpx.AsyncClient | None = None
        self._cb = _circuit_breaker

    # --- Lifecycle ----------------------------------------------------------

    async def __aenter__(self) -> "TansAIClient":
        self._client = self._build_client()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _build_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self.base_url,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            timeout=self.timeout,
            # HTTP/2 for multiplexing + lower latency (#2)
            http2=True,
            # Connection pool — keep 20 connections alive for 60s (#2)
            limits=httpx.Limits(
                max_keepalive_connections=20,
                max_connections=40,
                keepalive_expiry=60,
            ),
        )

    def _require_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = self._build_client()
        return self._client

    # --- Internal retry helper (#3) -----------------------------------------

    async def _request_with_retry(
        self,
        method: str,
        path: str,
        **kwargs: Any,
    ) -> httpx.Response:
        """Execute an HTTP request with exponential-backoff retry.

        Raises TansAIError on permanent failure or circuit-open.
        """
        # Circuit breaker check (#4)
        if await self._cb.is_open():
            raise TansAIError(
                "AI sedang sibuk, coba lagi dalam ~1 menit. "
                "(Circuit breaker terbuka — server tidak merespons berulang kali.)"
            )

        client = self._require_client()
        last_exc: Exception | None = None

        for attempt in range(1, self._MAX_RETRIES + 1):
            try:
                response = await client.request(method, path, **kwargs)

                if response.status_code in self._RETRYABLE_STATUS:
                    raise httpx.HTTPStatusError(
                        f"HTTP {response.status_code}",
                        request=response.request,
                        response=response,
                    )

                await self._cb.record_success()
                return response

            except (httpx.RequestError, httpx.HTTPStatusError) as exc:
                last_exc = exc
                await self._cb.record_failure()

                if attempt < self._MAX_RETRIES:
                    delay = self._RETRY_BASE_DELAY * (2 ** (attempt - 1))
                    logger.warning(
                        "Tans AI request failed (attempt %d/%d), retry in %.1fs: %s",
                        attempt,
                        self._MAX_RETRIES,
                        delay,
                        exc,
                    )
                    await asyncio.sleep(delay)
                else:
                    logger.error(
                        "Tans AI request failed after %d attempts: %s",
                        self._MAX_RETRIES,
                        exc,
                    )

        raise TansAIError(
            f"Tidak bisa terhubung ke Tans AI ({self.base_url}) "
            f"setelah {self._MAX_RETRIES} percobaan: {last_exc}"
        ) from last_exc

    # --- Public API ---------------------------------------------------------

    async def chat(self, message: str, model: str) -> str:
        """Send a message to /chat and return the reply text."""
        try:
            response = await self._request_with_retry(
                "POST", "/chat", json={"message": message, "model": model}
            )
        except TansAIError:
            raise
        except Exception as exc:
            raise TansAIError(str(exc)) from exc

        if response.status_code >= 400:
            raise TansAIError(
                f"Tans AI mengembalikan HTTP {response.status_code}: "
                f"{response.text[:500]}"
            )

        data = self._safe_json(response)
        return self._extract_reply(data)

    async def chat_stream(self, message: str, model: str):
        """Stream chat response token-by-token via SSE (#1).

        Yields text chunks as they arrive. Falls back gracefully if the
        server does not support streaming (yields full reply as one chunk).
        """
        if await self._cb.is_open():
            raise TansAIError(
                "AI sedang sibuk, coba lagi dalam ~1 menit."
            )

        client = self._require_client()
        payload = {"message": message, "model": model, "stream": True}

        try:
            async with client.stream("POST", "/chat", json=payload) as response:
                if response.status_code >= 400:
                    body = await response.aread()
                    raise TansAIError(
                        f"Tans AI HTTP {response.status_code}: {body[:500].decode()}"
                    )

                content_type = response.headers.get("content-type", "")
                if "text/event-stream" in content_type:
                    # True SSE streaming
                    async for line in response.aiter_lines():
                        if line.startswith("data:"):
                            chunk = line[5:].strip()
                            if chunk and chunk != "[DONE]":
                                yield chunk
                else:
                    # Server does not stream — yield full body as one chunk
                    body = await response.aread()
                    data = self._safe_json(response)
                    yield self._extract_reply(data)

            await self._cb.record_success()

        except TansAIError:
            raise
        except httpx.RequestError as exc:
            await self._cb.record_failure()
            raise TansAIError(
                f"Tidak bisa terhubung ke Tans AI ({self.base_url}): {exc}"
            ) from exc

    async def list_models(self) -> list[str]:
        """Fetch available models from /models."""
        response = await self._request_with_retry("GET", "/models")
        if response.status_code >= 400:
            raise TansAIError(
                f"Tans AI mengembalikan HTTP {response.status_code}: "
                f"{response.text[:500]}"
            )
        data = self._safe_json(response)
        return self._extract_models(data)

    async def status(self) -> dict[str, Any]:
        """Check API key validity via /status."""
        response = await self._request_with_retry("GET", "/status")
        if response.status_code >= 400:
            raise TansAIError(
                f"Tans AI mengembalikan HTTP {response.status_code}: "
                f"{response.text[:500]}"
            )
        data = self._safe_json(response)
        if isinstance(data, dict):
            return data
        return {"raw": data}

    # --- Circuit breaker info -----------------------------------------------

    @property
    def circuit_state(self) -> str:
        return self._cb.state

    # --- Helpers ------------------------------------------------------------

    @staticmethod
    def _safe_json(response: httpx.Response) -> Any:
        try:
            return response.json()
        except ValueError as exc:
            raise TansAIError(
                f"Response Tans AI bukan JSON valid: {response.text[:500]}"
            ) from exc

    @staticmethod
    def _extract_reply(data: Any) -> str:
        """Try to extract the assistant reply from common response shapes."""
        if isinstance(data, str):
            return data
        if isinstance(data, dict):
            for key in ("reply", "response", "message", "content", "text", "answer", "result"):
                value = data.get(key)
                if isinstance(value, str) and value.strip():
                    return value
            choices = data.get("choices")
            if isinstance(choices, list) and choices:
                first = choices[0]
                if isinstance(first, dict):
                    msg = first.get("message")
                    if isinstance(msg, dict):
                        content = msg.get("content")
                        if isinstance(content, str):
                            return content
                    text = first.get("text")
                    if isinstance(text, str):
                        return text
            data_field = data.get("data")
            if isinstance(data_field, dict):
                return TansAIClient._extract_reply(data_field)
        raise TansAIError(
            f"Format response Tans AI tidak dikenali: {str(data)[:500]}"
        )

    @staticmethod
    def _extract_models(data: Any) -> list[str]:
        """Best-effort extraction of model names from /models response."""
        candidates: list[Any] = []
        if isinstance(data, list):
            candidates = data
        elif isinstance(data, dict):
            for key in ("models", "data", "result", "items"):
                value = data.get(key)
                if isinstance(value, list):
                    candidates = value
                    break

        models: list[str] = []
        for item in candidates:
            if isinstance(item, str):
                models.append(item)
            elif isinstance(item, dict):
                for key in ("id", "name", "model", "slug"):
                    value = item.get(key)
                    if isinstance(value, str):
                        models.append(value)
                        break
        return models
