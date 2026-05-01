"""
Handle incoming text messages.

Intent detection (reminder / summary) runs concurrently via asyncio.gather.
Food logging is delegated to the nutrition agent which handles food search,
diary logging, and DB persistence in one agentic loop.
"""
import asyncio
import logging
from datetime import datetime, date
from zoneinfo import ZoneInfo

import phonenumbers
from phonenumbers import timezone as pn_tz
from sqlalchemy.orm import Session

from app.models import User, MealEntry, ConversationState
from app.schemas import FoodItem
from app.services import nutrition as nutrition_svc
from app.services import nutrition_agent
from app.services.whatsapp import send_text_message
from app.config import get_settings

logger = logging.getLogger(__name__)

VALID_MEAL_TYPES = {"breakfast", "lunch", "dinner", "snack"}

_FALLBACK_TZ = ZoneInfo("UTC")


def _tz_from_phone(phone_number: str) -> ZoneInfo:
    """Resolve timezone from phone number country code. Falls back to UTC."""
    try:
        pn = phonenumbers.parse(f"+{phone_number.lstrip('+')}")
        tzs = pn_tz.time_zones_for_number(pn)
        if tzs:
            return ZoneInfo(tzs[0])
    except Exception:
        logger.debug("Could not resolve timezone for %s, using UTC", phone_number)
    return _FALLBACK_TZ


def _infer_meal_type(phone_number: str) -> str:
    """Infer meal type from current time in the user's timezone."""
    tz = _tz_from_phone(phone_number)
    hour = datetime.now(tz).hour
    if 5 <= hour < 11:
        return "breakfast"
    elif 11 <= hour < 15:
        return "lunch"
    elif 15 <= hour < 19:
        return "snack"
    else:
        return "dinner"


def _normalize_phone(phone_number: str) -> str:
    return phone_number.strip().lstrip("+")


def _get_or_create_user(db: Session, phone_number: str) -> tuple[User, bool]:
    """Return (user, is_new) where is_new=True if the user was just created."""
    phone_number = _normalize_phone(phone_number)
    user = db.query(User).filter(User.phone_number == phone_number).first()
    if not user:
        user = User(phone_number=phone_number)
        db.add(user)
        db.commit()
        db.refresh(user)
        return user, True
    return user, False


def _get_or_create_state(db: Session, user: User) -> ConversationState:
    state = db.query(ConversationState).filter(ConversationState.user_id == user.id).first()
    if not state:
        state = ConversationState(user_id=user.id, state="idle")
        db.add(state)
        db.commit()
        db.refresh(state)
    return state


def _daily_summary(db: Session, user: User, phone_number: str) -> str:
    today = datetime.now(_tz_from_phone(phone_number)).date()
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


async def handle_text(db: Session, phone_number: str, text: str, ack_sent: bool = False):
    """Main entry point for text messages.

    Args:
        db: SQLAlchemy session.
        phone_number: Sender's phone number (may include leading +).
        text: Message text.
        ack_sent: True when the caller (e.g. voice handler) has already sent
            an acknowledgment message, so we skip sending a second one.
    """
    logger.debug("[%s] Incoming text: %r", phone_number, text)

    # Handle /info command — send bot instructions and stop
    if text.strip().lower() == "/info":
        await send_text_message(
            phone_number,
            "📖 *NutriBot — How It Works*\n\n"
            "*Logging meals*\n"
            "Just tell me what you ate in plain text or send a voice note:\n"
            "  • \"2 eggs and toast\"\n"
            "  • \"100g chicken breast with rice\"\n"
            "I'll look up the nutrition data and log it to your diary.\n\n"
            "*Daily summary*\n"
            "Ask for your totals any time:\n"
            "  • \"What's my total today?\"\n"
            "  • \"Show me today's summary\"\n\n"
            "*Reminders*\n"
            "Set meal reminders in natural language:\n"
            "  • \"Remind me every day at 8pm to log dinner\"\n\n"
            "*Commands*\n"
            "  • /connect — link your NutriChat account\n"
            "  • /info — show this message\n\n"
            "*Deleting entries*\n"
            "Remove logged entries for a meal or the whole day:\n"
            "  • \"delete lunch\"\n"
            "  • \"delete today\"\n"
            "  • \"clear my breakfast\"\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "*Connecting NutriChat (optional)*\n"
            "Link your NutriChat account to sync meals with the NutriChat app "
            "and get accurate nutrition data from a real food database.\n\n"
            "To connect, send:\n"
            "  link nutrichat_live_YOUR_API_KEY\n\n"
            "You can find your API key in the NutriChat app settings.",
        )
        return

    # Handle /connect command — send NutriChat linking instructions
    if text.strip().lower() == "/connect":
        await send_text_message(
            phone_number,
            "To connect your NutriChat account, send:\n"
            "  link nutrichat_live_YOUR_API_KEY\n\n"
            "You can find your API key in the NutriChat app settings.",
        )
        return

    # Handle NutriChat API key linking: "link nutrichat_live_xxxx"
    text_lower = text.strip().lower()
    if text_lower.startswith("link ") and "nutrichat" in text_lower:
        from nutrichat import NutriChatClient, AuthError as NCAuthError

        api_key = text.strip().split(None, 1)[1].strip()
        if not api_key.startswith("nutrichat_live_"):
            await send_text_message(
                phone_number,
                "Invalid key. It should start with 'nutrichat_live_'.",
            )
            return

        user, _ = _get_or_create_user(db, phone_number)
        nc_settings = get_settings()
        try:
            async with NutriChatClient(api_key=api_key, base_url=nc_settings.nutrichat_base_url) as client:
                await client.get_today_totals()
        except NCAuthError:
            await send_text_message(
                phone_number,
                "Invalid or expired API key. Generate a new one in the NutriChat app.",
            )
            return
        except Exception:
            logger.exception("[%s] NutriChat key validation failed", phone_number)
            await send_text_message(phone_number, "Could not verify key. Try again later.")
            return

        user.nutrichat_api_key = api_key
        db.commit()
        logger.info("[%s] NutriChat API key linked", phone_number)
        await send_text_message(
            phone_number,
            "✅ Linked! Your NutriChat app and this bot are now synced.\n\n"
            "Going forward I'll log all your meals directly to your NutriChat diary. "
            "Just tell me what you ate!",
        )
        return

    user, is_new = _get_or_create_user(db, phone_number)
    _get_or_create_state(db, user)

    if is_new:
        await send_text_message(
            phone_number,
            "👋 Welcome to NutriBot!\n\n"
            "I track your meals and macros automatically.\n\n"
            "Just tell me what you ate — I'll log the nutrition for you.\n\n"
            "To sync with the NutriChat app and get accurate food database lookups, send:\n"
            "  link nutrichat_live_YOUR_API_KEY\n\n"
            "You can find your API key in the NutriChat app settings.\n\n"
            "Send /info for a full list of commands.",
        )
        logger.info("[%s] Sent welcome message to new user", phone_number)

    # Detect all intents concurrently before sending any ack
    is_reminder, is_summary, is_delete = await asyncio.gather(
        asyncio.to_thread(nutrition_svc.is_reminder_request, text),
        asyncio.to_thread(nutrition_svc.is_summary_request, text),
        asyncio.to_thread(nutrition_svc.is_delete_request, text),
    )

    if is_reminder:
        await _handle_reminder(db, user, phone_number, text)
        return

    if is_summary:
        await send_text_message(phone_number, _daily_summary(db, user, phone_number))
        return

    if is_delete:
        await _handle_delete(db, user, phone_number, text)
        return

    # Food logging — send ack first, then run agent
    if not ack_sent:
        await send_text_message(
            phone_number,
            "🍽️ Got it! I'm logging your meal and crunching the numbers — "
            "you'll get a full breakdown with macros and your daily total in just a moment! 📊",
        )

    # Food logging via the nutrition agent
    # Check if user explicitly mentioned a meal type in the message
    meal_type = _infer_meal_type(phone_number)
    text_lower = text.strip().lower()
    for mt in ("breakfast", "lunch", "dinner", "snack"):
        if mt in text_lower:
            meal_type = mt
            break
    logger.debug("[%s] meal_type=%r for logging", phone_number, meal_type)

    # Reload user from DB to pick up any tokens that were added
    # while a voice note was being transcribed (race condition).
    db.refresh(user)

    try:
        reply = await nutrition_agent.run_nutrition_agent(text, user, meal_type, db)
    except Exception:
        logger.exception("[%s] Nutrition agent failed", phone_number)
        await send_text_message(
            phone_number,
            "Sorry, I couldn't log that meal. Please try again.",
        )
        return

    await send_text_message(phone_number, reply)


def _parse_delete_target(text: str) -> dict:
    """Return {'scope': 'meal', 'meal_type': X} or {'scope': 'day'}."""
    text_lower = text.lower()
    for meal in ("breakfast", "lunch", "dinner", "snack"):
        if meal in text_lower:
            return {"scope": "meal", "meal_type": meal}
    return {"scope": "day"}


async def _handle_delete(db: Session, user: User, phone_number: str, text: str):
    """Handle requests to delete meal entries for a specific meal type or the whole day.

    NutriChat is the source of truth — delete there first, then clean up local DB.
    """
    from app.services import nutrichat_svc as nc_svc
    from app.services.fatsecret import delete_food_entries as fs_delete_food_entries

    target = _parse_delete_target(text)
    today = datetime.now(_tz_from_phone(phone_number)).date()
    nc_meal_type = target["meal_type"] if target["scope"] == "meal" else None
    label = nc_meal_type or "today"

    # Delete from NutriChat first (source of truth)
    nc_deleted = 0
    if user.nutrichat_api_key:
        nc_deleted = await nc_svc.delete_food_entries(
            user.nutrichat_api_key,
            meal_type=nc_meal_type,
            target_date=today.isoformat(),
        )
        logger.info("[%s] Deleted %d NutriChat diary entries", phone_number, nc_deleted)

    # Also clean up local DB entries
    query = db.query(MealEntry).filter(
        MealEntry.user_id == user.id,
        MealEntry.logged_at >= datetime(today.year, today.month, today.day),
    )
    if target["scope"] == "meal":
        query = query.filter(MealEntry.meal_type == target["meal_type"])

    entries = query.all()

    # Legacy: delete from FatSecret if connected and no NutriChat
    if not user.nutrichat_api_key and user.fatsecret_access_token and user.fatsecret_access_secret:
        fs_ids = []
        for entry in entries:
            if entry.fatsecret_entry_id:
                fs_ids.extend(i.strip() for i in entry.fatsecret_entry_id.split(",") if i.strip())
        if fs_ids:
            fs_delete_food_entries(fs_ids, user.fatsecret_access_token, user.fatsecret_access_secret)

    db_count = len(entries)
    for entry in entries:
        db.delete(entry)
    db.commit()
    logger.info("[%s] Deleted %d local MealEntry rows", phone_number, db_count)

    total_deleted = nc_deleted or db_count
    if total_deleted == 0:
        await send_text_message(phone_number, f"No entries found for {label}.")
        return

    plural = "s" if total_deleted != 1 else ""
    if target["scope"] == "meal":
        msg = f"🗑️ Deleted {target['meal_type'].capitalize()} entries ({total_deleted} item{plural})."
    else:
        msg = f"🗑️ Deleted all entries for today ({total_deleted} item{plural})."

    await send_text_message(phone_number, msg)


async def _handle_reminder(db: Session, user: User, phone_number: str, text: str):
    from app.models import Reminder
    from app.services.scheduler import add_reminder_job

    try:
        config = nutrition_svc.parse_reminder(text)
    except Exception:
        logger.exception("[%s] Failed to parse reminder from %r", phone_number, text)
        await send_text_message(
            phone_number,
            "Sorry, I couldn't parse that reminder. Try: 'remind me at 8pm daily to log dinner'.",
        )
        return

    reminder = Reminder(
        user_id=user.id,
        label=config.label,
        cron_expression=config.cron_expression,
        message=config.message,
        active=True,
    )
    db.add(reminder)
    db.commit()
    db.refresh(reminder)

    add_reminder_job(reminder.id, user.phone_number, config.cron_expression, config.message)

    await send_text_message(
        phone_number,
        f"⏰ Reminder set! I'll remind you to log *{config.label}* on schedule: `{config.cron_expression}`",
    )
