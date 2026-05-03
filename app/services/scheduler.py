"""
APScheduler-based reminder service.

Reminders are persisted to the DB and re-loaded on startup.
Each reminder sends a message to the user at the scheduled time via the
correct platform (WhatsApp or Telegram).
"""
import asyncio
import logging
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)
scheduler = AsyncIOScheduler()


def _job_id(reminder_id: int) -> str:
    return f"reminder_{reminder_id}"


async def _send_reminder(recipient_id: str, platform: str, message: str):
    """Send a reminder via the appropriate messaging platform."""
    platform = platform or "whatsapp"
    try:
        if platform == "telegram":
            from app.services.telegram_messenger import send_text_message
            await send_text_message(recipient_id, message)
        else:
            from app.services.whatsapp import send_text_message
            await send_text_message(recipient_id, message)
    except Exception:
        logger.exception("Failed to send reminder to %s (%s)", recipient_id, platform)


def add_reminder_job(
    reminder_id: int,
    recipient_id: str,
    cron_expression: str,
    message: str,
    platform: str = "whatsapp",
):
    """Add or replace a scheduler job for a reminder."""
    parts = cron_expression.split()
    if len(parts) != 5:
        raise ValueError(f"Invalid cron expression: {cron_expression}")
    minute, hour, day, month, day_of_week = parts

    scheduler.add_job(
        _send_reminder,
        trigger=CronTrigger(
            minute=minute,
            hour=hour,
            day=day,
            month=month,
            day_of_week=day_of_week,
        ),
        args=[recipient_id, platform, message],
        id=_job_id(reminder_id),
        replace_existing=True,
    )


def remove_reminder_job(reminder_id: int):
    """Remove a scheduler job if it exists."""
    job_id = _job_id(reminder_id)
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)


def load_reminders_from_db(db: Session):
    """Load all active reminders from DB and register them with the scheduler."""
    from app.models import Reminder, User, TelegramUser

    reminders = db.query(Reminder).filter(Reminder.active == True).all()
    for reminder in reminders:
        user = db.query(User).filter(User.id == reminder.user_id).first()
        if not user:
            continue

        platform = reminder.platform or "whatsapp"
        if platform == "telegram":
            tg_user = db.query(TelegramUser).filter(TelegramUser.user_id == user.id).first()
            if not tg_user:
                logger.warning("Reminder %d has platform=telegram but no TelegramUser row", reminder.id)
                continue
            recipient_id = tg_user.chat_id
        else:
            recipient_id = user.phone_number

        try:
            add_reminder_job(reminder.id, recipient_id, reminder.cron_expression, reminder.message, platform=platform)
        except Exception:
            logger.exception("Failed to load reminder id=%d", reminder.id)


def start_scheduler(db: Session):
    """Start the scheduler and load existing reminders."""
    load_reminders_from_db(db)
    if not scheduler.running:
        scheduler.start()


def shutdown_scheduler():
    if scheduler.running:
        scheduler.shutdown(wait=False)
