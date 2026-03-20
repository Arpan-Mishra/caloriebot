import io
import openai
from app.config import get_settings

settings = get_settings()


async def transcribe_audio(audio_bytes: bytes, mime_type: str = "audio/ogg") -> str:
    """
    Transcribe audio bytes using OpenAI Whisper API.
    Returns the transcribed text string.
    """
    client = openai.AsyncOpenAI(api_key=settings.openai_api_key)

    ext_map = {
        "audio/ogg": "ogg",
        "audio/mpeg": "mp3",
        "audio/mp4": "mp4",
        "audio/wav": "wav",
        "audio/webm": "webm",
    }
    ext = ext_map.get(mime_type, "ogg")
    filename = f"audio.{ext}"

    audio_file = io.BytesIO(audio_bytes)
    audio_file.name = filename

    transcription = await client.audio.transcriptions.create(
        model="whisper-1",
        file=audio_file,
    )
    return transcription.text
