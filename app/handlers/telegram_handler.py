"""
Handle incoming Telegram webhook updates.

Telegram bot is standalone — no NutriChat/FatSecret. Macros are estimated
by the LLM agent and stored in local DB only.
Nutrition agent, transcription, and intent detection reused unchanged.
"""
import asyncio
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.models import User, MealEntry, TelegramUser, Reminder
from app.services import nutrition as nutrition_svc
from app.services import nutrition_agent
from app.services.transcription import transcribe_audio
from app.services.telegram_messenger import (
    download_media,
    parse_webhook_payload,
    send_text_message,
)

logger = logging.getLogger(__name__)

_VALID_MEAL_TYPES = {"breakfast", "lunch", "dinner", "snack"}


def _get_tz(tg_user: TelegramUser) -> ZoneInfo:
    if tg_user.timezone:
        try:
            return ZoneInfo(tg_user.timezone)
        except Exception:
            pass
    return ZoneInfo("UTC")


def _infer_meal_type(tg_user: TelegramUser) -> str:
    hour = datetime.now(_get_tz(tg_user)).hour
    if 5 <= hour < 11:
        return "breakfast"
    elif 11 <= hour < 15:
        return "lunch"
    elif 15 <= hour < 19:
        return "snack"
    return "dinner"


def _get_or_create_telegram_user(
    db: Session,
    chat_id: str,
    username: str | None,
    language_code: str | None,
) -> tuple[User, TelegramUser, bool]:
    """Return (user, tg_user, is_new). Creates both rows on first contact."""
    tg_user = db.query(TelegramUser).filter(TelegramUser.chat_id == chat_id).first()
    if tg_user:
        user = db.query(User).filter(User.id == tg_user.user_id).first()
        return user, tg_user, False

    synthetic_phone = f"tg:{chat_id}"
    user = User(phone_number=synthetic_phone)
    db.add(user)
    db.flush()

    tg_user = TelegramUser(
        user_id=user.id,
        chat_id=chat_id,
        username=username,
        language_code=language_code,
        timezone="UTC",
    )
    db.add(tg_user)
    db.commit()
    db.refresh(user)
    db.refresh(tg_user)
    logger.info("[tg:%s] Created new user @%s", chat_id, username)
    return user, tg_user, True


def _daily_summary(db: Session, user: User, tg_user: TelegramUser) -> str:
    today = datetime.now(_get_tz(tg_user)).date()
    entries = (
        db.query(MealEntry)
        .filter(
            MealEntry.user_id == user.id,
            MealEntry.logged_at >= datetime(today.year, today.month, today.day),
        )
        .all()
    )

    if not entries:
        return "No meals logged today yet. Send me what you ate!"

    total_cal = sum(e.calories or 0 for e in entries)
    total_pro = sum(e.protein_g or 0 for e in entries)
    total_fat = sum(e.fat_g or 0 for e in entries)
    total_carb = sum(e.carbs_g or 0 for e in entries)

    lines = [f"📅 *Today's Summary* ({today.strftime('%b %d')})", ""]
    for e in entries:
        if e.calories:
            lines.append(f"  • {e.meal_type.capitalize()}: {e.food_description} ({e.calories:.0f} kcal)")
        else:
            lines.append(f"  • {e.meal_type.capitalize()}: {e.food_description}")
    lines += [
        "",
        "*Totals:*",
        f"  Calories: {total_cal:.0f} kcal",
        f"  Protein:  {total_pro:.1f} g",
        f"  Carbs:    {total_carb:.1f} g",
        f"  Fat:      {total_fat:.1f} g",
    ]
    return "\n".join(lines)


async def _handle_delete(
    db: Session,
    user: User,
    tg_user: TelegramUser,
    chat_id: str,
    text: str,
):
    text_lower = text.lower()
    meal_type: str | None = None
    for meal in ("breakfast", "lunch", "dinner", "snack"):
        if meal in text_lower:
            meal_type = meal
            break

    today = datetime.now(_get_tz(tg_user)).date()
    label = meal_type or "today"

    query = db.query(MealEntry).filter(
        MealEntry.user_id == user.id,
        MealEntry.logged_at >= datetime(today.year, today.month, today.day),
    )
    if meal_type:
        query = query.filter(MealEntry.meal_type == meal_type)
    entries = query.all()
    count = len(entries)
    for entry in entries:
        db.delete(entry)
    db.commit()
    logger.info("[tg:%s] Deleted %d MealEntry rows", chat_id, count)

    if count == 0:
        await send_text_message(chat_id, f"No entries found for {label}.")
        return

    plural = "s" if count != 1 else ""
    if meal_type:
        await send_text_message(chat_id, f"🗑️ Deleted {meal_type.capitalize()} entries ({count} item{plural}).")
    else:
        await send_text_message(chat_id, f"🗑️ Deleted all entries for today ({count} item{plural}).")


async def _handle_reminder(db: Session, user: User, chat_id: str, text: str):
    from app.services.scheduler import add_reminder_job

    try:
        config = nutrition_svc.parse_reminder(text)
    except Exception:
        logger.exception("[tg:%s] Failed to parse reminder from %r", chat_id, text)
        await send_text_message(
            chat_id,
            "Sorry, I couldn't parse that reminder. Try: 'remind me at 8pm daily to log dinner'.",
        )
        return

    reminder = Reminder(
        user_id=user.id,
        label=config.label,
        cron_expression=config.cron_expression,
        message=config.message,
        active=True,
        platform="telegram",
    )
    db.add(reminder)
    db.commit()
    db.refresh(reminder)

    add_reminder_job(reminder.id, chat_id, config.cron_expression, config.message, platform="telegram")
    await send_text_message(
        chat_id,
        f"⏰ Reminder set! I'll remind you to log *{config.label}* on schedule: `{config.cron_expression}`",
    )


async def handle_text_telegram(
    db: Session,
    chat_id: str,
    user: User,
    tg_user: TelegramUser,
    text: str,
    ack_sent: bool = False,
):
    logger.debug("[tg:%s] Incoming text: %r", chat_id, text)
    text_lower = text.strip().lower()

    if text_lower in ("/start", "/info"):
        await send_text_message(
            chat_id,
            "📖 *NutriBot — How It Works*\n\n"
            "*Logging meals*\n"
            "Just tell me what you ate in plain text or send a voice note:\n"
            "  • \"2 eggs and toast\"\n"
            "  • \"100g chicken breast with rice\"\n"
            "I'll estimate the nutrition and log it to your diary.\n\n"
            "*Daily summary*\n"
            "Ask for your totals any time:\n"
            "  • \"What's my total today?\"\n"
            "  • \"Show me today's summary\"\n\n"
            "*Reminders*\n"
            "Set meal reminders in natural language:\n"
            "  • \"Remind me every day at 8pm to log dinner\"\n\n"
            "*Commands*\n"
            "  • /timezone — set your timezone (e.g. `/timezone Asia/Kolkata`)\n"
            "  • /info — show this message\n\n"
            "*Deleting entries*\n"
            "Remove logged entries for a meal or the whole day:\n"
            "  • \"delete lunch\"\n"
            "  • \"delete today\"\n"
            "  • \"clear my breakfast\"",
        )
        return

    # /timezone <iana_name>
    if text_lower.startswith("/timezone"):
        tz_name = text.strip()[len("/timezone"):].strip()
        if not tz_name:
            await send_text_message(
                chat_id,
                "Usage: `/timezone Asia/Kolkata`\n\nExamples: `Asia/Kolkata`, `Europe/London`, `America/New_York`",
            )
            return
        try:
            ZoneInfo(tz_name)
        except Exception:
            await send_text_message(
                chat_id,
                f"Unknown timezone '{tz_name}'.\nUse a valid IANA name, e.g. `Asia/Kolkata`, `Europe/London`, `America/New_York`.",
            )
            return
        tg_user.timezone = tz_name
        db.commit()
        logger.info("[tg:%s] Timezone set to %s", chat_id, tz_name)
        await send_text_message(chat_id, f"✅ Timezone set to {tz_name}.")
        return

    # Concurrent intent detection
    is_reminder, is_summary, is_delete = await asyncio.gather(
        asyncio.to_thread(nutrition_svc.is_reminder_request, text),
        asyncio.to_thread(nutrition_svc.is_summary_request, text),
        asyncio.to_thread(nutrition_svc.is_delete_request, text),
    )

    if is_reminder:
        await _handle_reminder(db, user, chat_id, text)
        return

    if is_summary:
        await send_text_message(chat_id, _daily_summary(db, user, tg_user))
        return

    if is_delete:
        await _handle_delete(db, user, tg_user, chat_id, text)
        return

    # Food logging — LLM estimates only (no external food DB on Telegram)
    if not ack_sent:
        await send_text_message(
            chat_id,
            "🍽️ Got it! I'm logging your meal and crunching the numbers — "
            "you'll get a full breakdown with macros and your daily total in just a moment! 📊",
        )

    meal_type = _infer_meal_type(tg_user)
    for mt in _VALID_MEAL_TYPES:
        if mt in text_lower:
            meal_type = mt
            break
    logger.debug("[tg:%s] meal_type=%r for logging", chat_id, meal_type)

    db.refresh(user)
    try:
        reply = await nutrition_agent.run_nutrition_agent(text, user, meal_type, db)
    except Exception:
        logger.exception("[tg:%s] Nutrition agent failed", chat_id)
        await send_text_message(chat_id, "Sorry, I couldn't log that meal. Please try again.")
        return

    await send_text_message(chat_id, reply)


async def handle_voice_telegram(
    db: Session,
    chat_id: str,
    user: User,
    tg_user: TelegramUser,
    file_id: str,
    mime_type: str = "audio/ogg",
):
    ack_coro = send_text_message(
        chat_id,
        "🎙️ Voice note received! I'm transcribing and logging your meal — "
        "you'll get a full breakdown with macros and your daily total in just a moment! 📊",
    )
    download_coro = download_media(file_id)

    try:
        results = await asyncio.gather(ack_coro, download_coro)
        audio_bytes, detected_mime = results[1]
    except Exception:
        logger.exception("[tg:%s] Failed to download voice note (file_id=%s)", chat_id, file_id)
        try:
            await send_text_message(chat_id, "Sorry, I couldn't download your voice note. Please try again.")
        except Exception:
            logger.exception("[tg:%s] Failed to send error message", chat_id)
        return

    try:
        transcribed_text = await transcribe_audio(audio_bytes, detected_mime)
        logger.debug("[tg:%s] Transcript: %r", chat_id, transcribed_text)
    except Exception:
        logger.exception("[tg:%s] Failed to transcribe voice note", chat_id)
        await send_text_message(
            chat_id,
            "Sorry, I couldn't transcribe your voice note. Please try sending a text message instead.",
        )
        return

    await handle_text_telegram(db, chat_id, user, tg_user, transcribed_text, ack_sent=True)


async def route_telegram_webhook(body: dict, db: Session):
    """Main entry point for incoming Telegram updates."""
    messages = parse_webhook_payload(body)
    for msg in messages:
        chat_id = msg.get("from_id", "")
        if not chat_id:
            continue

        user, tg_user, is_new = _get_or_create_telegram_user(
            db, chat_id, msg.get("username"), msg.get("language_code")
        )

        if is_new:
            await send_text_message(
                chat_id,
                "👋 Welcome to NutriBot!\n\n"
                "I track your meals and macros automatically.\n\n"
                "Just tell me what you ate — I'll estimate the nutrition and log it.\n\n"
                "Set your timezone so meal times are recognised correctly:\n"
                "  `/timezone Asia/Kolkata`\n\n"
                "Send /info for a full list of commands.",
            )
            if msg.get("type") == "text" and msg.get("text", "").strip().lower() == "/start":
                continue

        msg_type = msg.get("type")
        if msg_type == "text":
            await handle_text_telegram(db, chat_id, user, tg_user, msg["text"])
        elif msg_type == "voice":
            await handle_voice_telegram(
                db, chat_id, user, tg_user, msg["file_id"], msg.get("mime_type", "audio/ogg")
            )
        else:
            logger.debug("[tg:%s] Ignoring unsupported message type: %r", chat_id, msg_type)
