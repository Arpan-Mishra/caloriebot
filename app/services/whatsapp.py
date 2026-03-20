import httpx
from app.config import get_settings

settings = get_settings()

GRAPH_API_BASE = "https://graph.facebook.com/v19.0"


async def send_text_message(to: str, text: str) -> dict:
    """Send a plain text WhatsApp message."""
    url = f"{GRAPH_API_BASE}/{settings.whatsapp_phone_number_id}/messages"
    headers = {
        "Authorization": f"Bearer {settings.whatsapp_access_token}",
        "Content-Type": "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": text},
    }
    async with httpx.AsyncClient() as client:
        resp = await client.post(url, json=payload, headers=headers)
        resp.raise_for_status()
        return resp.json()


async def download_media(media_id: str) -> bytes:
    """Download a media file from WhatsApp Cloud API."""
    headers = {"Authorization": f"Bearer {settings.whatsapp_access_token}"}

    # Step 1: Get the download URL
    async with httpx.AsyncClient() as client:
        meta_resp = await client.get(
            f"{GRAPH_API_BASE}/{media_id}",
            headers=headers,
        )
        meta_resp.raise_for_status()
        media_url = meta_resp.json()["url"]

        # Step 2: Download the actual file
        file_resp = await client.get(media_url, headers=headers)
        file_resp.raise_for_status()
        return file_resp.content


def parse_webhook_payload(body: dict) -> list[dict]:
    """
    Parse the WhatsApp webhook payload and return a list of message dicts with:
      - from_number
      - type  ("text" | "audio" | ...)
      - text  (for text messages)
      - media_id + mime_type (for audio)
    """
    messages = []
    for entry in body.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            for msg in value.get("messages", []):
                parsed = {
                    "from_number": msg.get("from"),
                    "type": msg.get("type"),
                    "message_id": msg.get("id"),
                }
                if msg["type"] == "text":
                    parsed["text"] = msg.get("text", {}).get("body", "")
                elif msg["type"] == "audio":
                    audio = msg.get("audio", {})
                    parsed["media_id"] = audio.get("id")
                    parsed["mime_type"] = audio.get("mime_type", "audio/ogg")
                messages.append(parsed)
    return messages
