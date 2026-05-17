"""HTTP client for the Tans AI API Gateway."""
from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class TansAIError(Exception):
    """Raised when the Tans AI API returns an error or is unreachable."""


class TansAIClient:
    def __init__(self, base_url: str, api_key: str, timeout: float = 60.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "TansAIClient":
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            timeout=self.timeout,
        )
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _require_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                timeout=self.timeout,
            )
        return self._client

    async def chat(self, message: str, model: str) -> str:
        """Send a single message to the Tans AI /chat endpoint and return the reply text."""
        client = self._require_client()
        payload: dict[str, Any] = {"message": message, "model": model}
        try:
            response = await client.post("/chat", json=payload)
        except httpx.RequestError as exc:
            raise TansAIError(
                f"Tidak bisa terhubung ke Tans AI ({self.base_url}): {exc}"
            ) from exc

        if response.status_code >= 400:
            raise TansAIError(
                f"Tans AI mengembalikan HTTP {response.status_code}: "
                f"{response.text[:500]}"
            )

        data = self._safe_json(response)
        return self._extract_reply(data)

    async def list_models(self) -> list[str]:
        """Fetch available models from /models. Returns a list of model identifiers."""
        client = self._require_client()
        try:
            response = await client.get("/models")
        except httpx.RequestError as exc:
            raise TansAIError(
                f"Tidak bisa terhubung ke Tans AI ({self.base_url}): {exc}"
            ) from exc

        if response.status_code >= 400:
            raise TansAIError(
                f"Tans AI mengembalikan HTTP {response.status_code}: "
                f"{response.text[:500]}"
            )

        data = self._safe_json(response)
        return self._extract_models(data)

    async def status(self) -> dict[str, Any]:
        """Check API key validity via /status. Returns the raw status payload."""
        client = self._require_client()
        try:
            response = await client.get("/status")
        except httpx.RequestError as exc:
            raise TansAIError(
                f"Tidak bisa terhubung ke Tans AI ({self.base_url}): {exc}"
            ) from exc

        if response.status_code >= 400:
            raise TansAIError(
                f"Tans AI mengembalikan HTTP {response.status_code}: "
                f"{response.text[:500]}"
            )

        data = self._safe_json(response)
        if isinstance(data, dict):
            return data
        return {"raw": data}

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
        """Try to extract the assistant reply from a few common response shapes."""
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
