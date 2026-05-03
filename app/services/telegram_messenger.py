"""Telegram Bot API client — mirrors the shape of services/whatsapp.py."""
import logging
import httpx
from app.config import get_settings

logger = logging.getLogger(__name__)

TELEGRAM_API_BASE = "https://api.telegram.org"

_http_client: httpx.AsyncClient | None = None


def _get_http_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None:
        _http_client = httpx.AsyncClient(timeout=30.0)
    return _http_client


def _bot_url(path: str) -> str:
    token = get_settings().telegram_bot_token
    return f"{TELEGRAM_API_BASE}/bot{token}/{path}"


async def send_text_message(chat_id: str | int, text: str) -> dict:
    """Send a plain text message to a Telegram chat."""
    client = _get_http_client()
    resp = await client.post(
        _bot_url("sendMessage"),
        json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
    )
    if not resp.is_success:
        # Retry without parse_mode if Markdown caused a 400 (e.g. food names with special chars)
        logger.warning(
            "[tg:%s] sendMessage failed (status=%d), retrying as plain text", chat_id, resp.status_code
        )
        resp = await client.post(
            _bot_url("sendMessage"),
            json={"chat_id": chat_id, "text": text},
        )
    resp.raise_for_status()
    return resp.json()


async def download_media(file_id: str) -> tuple[bytes, str]:
    """Download a Telegram file by file_id. Returns (bytes, mime_type)."""
    client = _get_http_client()

    # Step 1: resolve the file path
    meta_resp = await client.get(_bot_url("getFile"), params={"file_id": file_id})
    meta_resp.raise_for_status()
    file_path = meta_resp.json()["result"]["file_path"]

    # Step 2: download the actual bytes
    token = get_settings().telegram_bot_token
    file_url = f"{TELEGRAM_API_BASE}/file/bot{token}/{file_path}"
    file_resp = await client.get(file_url)
    file_resp.raise_for_status()
    return file_resp.content, "audio/ogg"


def parse_webhook_payload(body: dict) -> list[dict]:
    """Parse a Telegram Update JSON body.

    Returns a list of normalized message dicts:
      - from_id       (str)  — sender chat id
      - type          (str)  — "text" | "voice" | "unknown"
      - text          (str)  — message body (text messages)
      - file_id       (str)  — Telegram file id (voice messages)
      - mime_type     (str)  — detected mime (voice messages)
      - username      (str | None)
      - language_code (str | None)
      - message_id    (int)
    """
    message = body.get("message") or body.get("edited_message")
    if not message:
        return []

    sender = message.get("from", {})
    chat = message.get("chat", {})
    from_id = str(chat.get("id") or sender.get("id", ""))
    username = sender.get("username")
    language_code = sender.get("language_code")
    message_id = message.get("message_id")

    parsed: dict = {
        "from_id": from_id,
        "username": username,
        "language_code": language_code,
        "message_id": message_id,
    }

    if "text" in message:
        parsed["type"] = "text"
        parsed["text"] = message["text"]
    elif "voice" in message or "audio" in message:
        voice = message.get("voice") or message.get("audio", {})
        parsed["type"] = "voice"
        parsed["file_id"] = voice.get("file_id", "")
        parsed["mime_type"] = voice.get("mime_type", "audio/ogg")
    else:
        parsed["type"] = "unknown"

    return [parsed]


async def set_webhook(url: str, secret_token: str = "") -> dict:
    """Register the Telegram webhook URL with Telegram's Bot API."""
    client = _get_http_client()
    payload: dict = {"url": url}
    if secret_token:
        payload["secret_token"] = secret_token
    resp = await client.post(_bot_url("setWebhook"), json=payload)
    resp.raise_for_status()
    result = resp.json()
    logger.info("Telegram setWebhook → %s", result)
    return result
