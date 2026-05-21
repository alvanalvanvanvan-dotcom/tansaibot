"""Voice helpers: speech-to-text (STT) + text-to-speech (TTS).

The Tans AI gateway used by this bot is text-in/text-out, so voice is wired
up against an OpenAI-compatible API (``/audio/transcriptions`` and
``/audio/speech``). It is fully optional: when ``VOICE_API_KEY`` is empty the
bot simply tells voice users to type instead.

Fallback TTS: gTTS (Google Text-to-Speech) is used automatically when
``VOICE_API_KEY`` is not configured. gTTS is free, no API key needed.
"""
from __future__ import annotations

import asyncio
import io
import logging
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

# gTTS — free fallback TTS (no API key needed)
try:
    from gtts import gTTS as _gTTS
    GTTS_AVAILABLE = True
except ImportError:
    GTTS_AVAILABLE = False
    logger.debug("gTTS not installed — free TTS fallback unavailable. pip install gTTS")


async def synthesize_gtts(text: str, lang: str = "id") -> bytes:
    """Buat audio dari teks menggunakan gTTS (Google TTS — gratis, tanpa API key).

    Args:
        text: Teks yang akan diubah menjadi suara (maks 3000 karakter).
        lang: Kode bahasa ISO 639-1. Default 'id' (Indonesia).
              Deteksi otomatis: jika teks mengandung banyak huruf non-ASCII
              atau kata-kata Inggris, gunakan 'id' untuk aksen netral.

    Returns:
        bytes: Audio MP3 siap dikirim sebagai Telegram voice.

    Raises:
        VoiceError: Jika gTTS tidak terinstall atau request gagal.
    """
    if not GTTS_AVAILABLE:
        raise VoiceError(
            "gTTS tidak terinstall. Jalankan: pip install gTTS"
        )
    if not text.strip():
        raise VoiceError("Teks kosong — tidak bisa diubah ke suara.")

    text = text[:3000]

    # Bersihkan teks dari format Markdown / HTML agar suara lebih natural
    import re
    clean = re.sub(r"<[^>]+>", "", text)          # strip HTML tags
    clean = re.sub(r"[*_`#~]+", "", clean)         # strip Markdown
    clean = re.sub(r"https?://\S+", "link", clean) # ganti URL dengan 'link'
    clean = clean.strip()
    if not clean:
        raise VoiceError("Teks kosong setelah pembersihan.")

    def _run_gtts() -> bytes:
        tts = _gTTS(text=clean, lang=lang, slow=False)
        buf = io.BytesIO()
        tts.write_to_fp(buf)
        buf.seek(0)
        return buf.read()

    try:
        audio_bytes = await asyncio.to_thread(_run_gtts)
        return audio_bytes
    except Exception as exc:
        raise VoiceError(f"gTTS gagal: {exc}") from exc



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
