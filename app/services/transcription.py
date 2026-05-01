"""Audio transcription via Groq Whisper (primary) with OpenAI fallback."""
import io
import logging
import time

import openai

from app.config import get_settings

logger = logging.getLogger(__name__)

_groq_client: openai.AsyncOpenAI | None = None
_openai_client: openai.AsyncOpenAI | None = None

MIME_TO_EXT = {
    "audio/ogg": "ogg",
    "audio/mpeg": "mp3",
    "audio/mp4": "mp4",
    "audio/wav": "wav",
    "audio/webm": "webm",
}


def _get_groq_client() -> openai.AsyncOpenAI:
    global _groq_client
    if _groq_client is None:
        settings = get_settings()
        _groq_client = openai.AsyncOpenAI(
            api_key=settings.groq_api_key,
            base_url="https://api.groq.com/openai/v1",
        )
    return _groq_client


def _get_openai_client() -> openai.AsyncOpenAI:
    global _openai_client
    if _openai_client is None:
        settings = get_settings()
        _openai_client = openai.AsyncOpenAI(api_key=settings.openai_api_key)
    return _openai_client


async def _transcribe_with_client(
    client: openai.AsyncOpenAI,
    model: str,
    audio_bytes: bytes,
    mime_type: str,
) -> str:
    """Run transcription against a single provider."""
    ext = MIME_TO_EXT.get(mime_type, "ogg")
    audio_file = io.BytesIO(audio_bytes)
    audio_file.name = f"audio.{ext}"
    transcription = await client.audio.transcriptions.create(
        model=model,
        file=audio_file,
    )
    return transcription.text


async def transcribe_audio(audio_bytes: bytes, mime_type: str = "audio/ogg") -> str:
    """Transcribe audio bytes. Uses Groq (fast) with OpenAI fallback."""
    settings = get_settings()
    audio_size_kb = len(audio_bytes) / 1024
    logger.debug("Transcribing audio: %.1f KB, mime=%s", audio_size_kb, mime_type)

    # Try Groq first (much faster)
    if settings.groq_api_key:
        try:
            start = time.monotonic()
            text = await _transcribe_with_client(
                _get_groq_client(), "whisper-large-v3-turbo", audio_bytes, mime_type,
            )
            elapsed = time.monotonic() - start
            logger.info("Groq transcription completed in %.2fs (%.1f KB)", elapsed, audio_size_kb)
            return text
        except Exception:
            logger.exception("Groq transcription failed, falling back to OpenAI")

    # Fallback to OpenAI Whisper
    start = time.monotonic()
    text = await _transcribe_with_client(
        _get_openai_client(), "whisper-1", audio_bytes, mime_type,
    )
    elapsed = time.monotonic() - start
    logger.info("OpenAI transcription completed in %.2fs (%.1f KB)", elapsed, audio_size_kb)
    return text
