"""
Route incoming webhook events to the appropriate handler.
"""
from sqlalchemy.orm import Session
from app.services.whatsapp import parse_webhook_payload
from app.handlers.text_handler import handle_text
from app.handlers.voice_handler import handle_voice


async def route_webhook(body: dict, db: Session):
    messages = parse_webhook_payload(body)
    for msg in messages:
        phone_number = msg["from_number"]
        msg_type = msg["type"]

        if msg_type == "text":
            await handle_text(db, phone_number, msg["text"])
        elif msg_type == "audio":
            await handle_voice(db, phone_number, msg["media_id"], msg.get("mime_type", "audio/ogg"))
        # Other types (image, document, etc.) are silently ignored for now
