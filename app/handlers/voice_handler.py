"""
Handle incoming voice/audio messages.

1. Send acknowledgment and download audio concurrently.
2. Transcribe with Groq Whisper (fast) or OpenAI fallback.
3. Delegate to text_handler (which runs the nutrition agent).
"""
import asyncio
import logging
from sqlalchemy.orm import Session
from app.services.whatsapp import download_media, send_text_message
from app.services.transcription import transcribe_audio
from app.handlers.text_handler import handle_text

logger = logging.getLogger(__name__)


async def handle_voice(db: Session, phone_number: str, media_id: str, mime_type: str = "audio/ogg"):
    # Send ack and download audio concurrently
    ack_coro = send_text_message(
        phone_number,
        "🎙️ Voice note received! I'm transcribing and logging your meal — "
        "you'll get a full breakdown with macros and your daily total in just a moment! 📊",
    )
    download_coro = download_media(media_id)

    try:
        _, audio_bytes = await asyncio.gather(ack_coro, download_coro)
    except Exception:
        logger.exception("Failed to download voice note (media_id=%s)", media_id)
        try:
            await send_text_message(
                phone_number, "Sorry, I couldn't download your voice note. Please try again."
            )
        except Exception:
            logger.exception("Failed to send error message to %s", phone_number)
        return

    # Transcribe
    try:
        transcribed_text = await transcribe_audio(audio_bytes, mime_type)
        logger.debug("[%s] Transcript: %r", phone_number, transcribed_text)
    except Exception:
        logger.exception("Failed to transcribe voice note (media_id=%s)", media_id)
        await send_text_message(
            phone_number,
            "Sorry, I couldn't transcribe your voice note. Please try sending a text message instead.",
        )
        return

    # Delegate to text handler; ack already sent so skip the second one
    await handle_text(db, phone_number, transcribed_text, ack_sent=True)
