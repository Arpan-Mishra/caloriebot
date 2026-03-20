"""
Handle incoming text messages.

Intent detection (reminder / summary) runs concurrently via asyncio.gather.
Food logging is delegated to the nutrition agent which handles FatSecret search,
diary logging, and DB persistence in one agentic loop.
"""
import asyncio
import logging
from datetime import datetime, date
from sqlalchemy.orm import Session

from app.models import User, MealEntry, ConversationState
from app.schemas import FoodItem
from app.services import nutrition as nutrition_svc
from app.services import nutrition_agent
from app.services.whatsapp import send_text_message
from app.config import get_settings

logger = logging.getLogger(__name__)

VALID_MEAL_TYPES = {"breakfast", "lunch", "dinner", "snack"}


def _infer_meal_type() -> str:
    """Infer meal type from current local time."""
    hour = datetime.now().hour
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


def _daily_summary(db: Session, user: User) -> str:
    today = date.today()
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
        settings = get_settings()
        normalized = _normalize_phone(phone_number)
        connect_url = f"{settings.app_base_url.rstrip('/')}/connect/fatsecret?phone_number={normalized}"
        await send_text_message(
            phone_number,
            "📖 *CalorieBot — How It Works*\n\n"
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
            "  • /connect — link your FatSecret account\n"
            "  • /info — show this message\n\n"
            "*Deleting entries*\n"
            "Remove logged entries for a meal or the whole day:\n"
            "  • \"delete lunch\"\n"
            "  • \"delete today\"\n"
            "  • \"clear my breakfast\"\n"
            "(Deletions also remove entries from your FatSecret diary if connected.)\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "*Connecting FatSecret (optional)*\n"
            "FatSecret gives you accurate nutrition data from a real food database instead of AI estimates.\n\n"
            f"Link your account: {connect_url}\n\n"
            "⚠️ *Important:* On the FatSecret page you must *log in* with email + password.\n"
            "Do *not* use \"Sign in with Google\" — it won't redirect back to the bot.\n\n"
            "No FatSecret account yet? Create one at fatsecret.com first (use email, not Google), "
            "then tap the link above.",
        )
        return

    # Handle /connect command — send OAuth link and stop, no food logging
    if text.strip().lower() == "/connect":
        settings = get_settings()
        normalized = _normalize_phone(phone_number)
        connect_url = f"{settings.app_base_url.rstrip('/')}/connect/fatsecret?phone_number={normalized}"
        await send_text_message(
            phone_number,
            f"Tap this link to connect your FatSecret account:\n{connect_url}",
        )
        return

    user, is_new = _get_or_create_user(db, phone_number)
    _get_or_create_state(db, user)

    if is_new:
        settings = get_settings()
        normalized = _normalize_phone(phone_number)
        connect_url = f"{settings.app_base_url.rstrip('/')}/connect/fatsecret?phone_number={normalized}"
        await send_text_message(
            phone_number,
            f"👋 Welcome to CalorieBot!\n\n"
            f"I track your meals and macros automatically.\n\n"
            f"To get the most accurate nutrition data, connect your FatSecret account:\n"
            f"{connect_url}\n\n"
            f"You can still log meals without connecting — I'll estimate macros using AI. "
            f"Connect FatSecret any time for database-accurate values.",
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
        await send_text_message(phone_number, _daily_summary(db, user))
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
    meal_type = _infer_meal_type()
    logger.debug("[%s] Inferred meal_type=%r from time", phone_number, meal_type)

    # Reload user from DB to pick up any FatSecret tokens that were added
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
    """Handle requests to delete meal entries for a specific meal type or the whole day."""
    from app.services.fatsecret import delete_food_entries

    target = _parse_delete_target(text)
    today = date.today()
    query = db.query(MealEntry).filter(
        MealEntry.user_id == user.id,
        MealEntry.logged_at >= datetime(today.year, today.month, today.day),
    )
    if target["scope"] == "meal":
        query = query.filter(MealEntry.meal_type == target["meal_type"])

    entries = query.all()
    if not entries:
        label = target["meal_type"] if target["scope"] == "meal" else "today"
        await send_text_message(phone_number, f"No entries found for {label}.")
        return

    # Collect FatSecret entry IDs (stored as comma-separated string per MealEntry row)
    fs_ids = []
    for entry in entries:
        if entry.fatsecret_entry_id:
            fs_ids.extend(i.strip() for i in entry.fatsecret_entry_id.split(",") if i.strip())

    # Delete from FatSecret if connected
    fs_deleted = 0
    if fs_ids and user.fatsecret_access_token and user.fatsecret_access_secret:
        fs_deleted = delete_food_entries(fs_ids, user.fatsecret_access_token, user.fatsecret_access_secret)
        logger.info("[%s] Deleted %d FatSecret entries", phone_number, fs_deleted)

    # Delete from local DB
    db_count = len(entries)
    for entry in entries:
        db.delete(entry)
    db.commit()
    logger.info("[%s] Deleted %d local MealEntry rows", phone_number, db_count)

    plural = "s" if db_count != 1 else ""
    if target["scope"] == "meal":
        msg = f"🗑️ Deleted {target['meal_type'].capitalize()} entries ({db_count} item{plural}"
    else:
        msg = f"🗑️ Deleted all entries for today ({db_count} item{plural}"

    if fs_deleted:
        msg += ", removed from FatSecret diary too"
    msg += ")."

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
