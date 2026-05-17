"""Voice helpers: speech-to-text (STT) + text-to-speech (TTS).

The Tans AI gateway used by this bot is text-in/text-out, so voice is wired
up against an OpenAI-compatible API (``/audio/transcriptions`` and
``/audio/speech``). It is fully optional: when ``VOICE_API_KEY`` is empty the
bot simply tells voice users to type instead.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)


class VoiceError(Exception):
    """Raised when an STT/TTS call fails."""


@dataclass(frozen=True)
class VoiceConfig:
    api_base: str
    api_key: str
    stt_model: str
    tts_model: str
    tts_voice: str
    timeout: float

    @property
    def enabled(self) -> bool:
        return bool(self.api_key and self.api_base)


class VoiceClient:
    def __init__(self, config: VoiceConfig) -> None:
        self.config = config
        self._client: httpx.AsyncClient | None = None

    @property
    def enabled(self) -> bool:
        return self.config.enabled

    def _require_client(self) -> httpx.AsyncClient:
        if not self.config.enabled:
            raise VoiceError(
                "Voice belum dikonfigurasi. Set VOICE_API_BASE & VOICE_API_KEY di .env."
            )
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.config.api_base.rstrip("/"),
                headers={"Authorization": f"Bearer {self.config.api_key}"},
                timeout=self.config.timeout,
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def transcribe(self, audio_bytes: bytes, filename: str = "audio.ogg") -> str:
        client = self._require_client()
        mime = _guess_mime(filename)
        files = {"file": (filename, audio_bytes, mime)}
        data = {"model": self.config.stt_model}
        try:
            response = await client.post(
                "/audio/transcriptions", files=files, data=data
            )
        except httpx.RequestError as exc:
            raise VoiceError(f"STT request gagal: {exc}") from exc
        if response.status_code >= 400:
            raise VoiceError(
                f"STT HTTP {response.status_code}: {response.text[:300]}"
            )
        payload = _safe_json(response)
        if isinstance(payload, dict):
            for key in ("text", "transcript"):
                value = payload.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        raise VoiceError(
            f"Format response STT tidak dikenali: {str(payload)[:300]}"
        )

    async def synthesize(self, text: str) -> bytes:
        client = self._require_client()
        # OpenAI TTS rejects empty/long inputs. Keep it conservative.
        if not text:
            raise VoiceError("Teks kosong untuk TTS.")
        text = text[:3000]
        payload = {
            "model": self.config.tts_model,
            "voice": self.config.tts_voice,
            "input": text,
            "response_format": "opus",
        }
        try:
            response = await client.post("/audio/speech", json=payload)
        except httpx.RequestError as exc:
            raise VoiceError(f"TTS request gagal: {exc}") from exc
        if response.status_code >= 400:
            raise VoiceError(
                f"TTS HTTP {response.status_code}: {response.text[:300]}"
            )
        if not response.content:
            raise VoiceError("TTS mengembalikan body kosong.")
        return response.content


def _guess_mime(name: str) -> str:
    name = name.lower()
    if name.endswith(".oga") or name.endswith(".ogg"):
        return "audio/ogg"
    if name.endswith(".mp3"):
        return "audio/mpeg"
    if name.endswith(".m4a"):
        return "audio/mp4"
    if name.endswith(".wav"):
        return "audio/wav"
    if name.endswith(".webm"):
        return "audio/webm"
    return "application/octet-stream"


def _safe_json(response: httpx.Response):
    try:
        return response.json()
    except ValueError as exc:  # noqa: F841
        return None
