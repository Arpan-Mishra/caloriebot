"""
APScheduler-based reminder service.

Reminders are persisted to the DB and re-loaded on startup.
Each reminder sends a WhatsApp message to the user at the scheduled time.
"""
import asyncio
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy.orm import Session

scheduler = AsyncIOScheduler()


def _job_id(reminder_id: int) -> str:
    return f"reminder_{reminder_id}"


async def _send_reminder(phone_number: str, message: str):
    from app.services.whatsapp import send_text_message
    await send_text_message(phone_number, message)


def add_reminder_job(reminder_id: int, phone_number: str, cron_expression: str, message: str):
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
        args=[phone_number, message],
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
    from app.models import Reminder, User

    reminders = db.query(Reminder).filter(Reminder.active == True).all()
    for reminder in reminders:
        user = db.query(User).filter(User.id == reminder.user_id).first()
        if user:
            try:
                add_reminder_job(
                    reminder.id,
                    user.phone_number,
                    reminder.cron_expression,
                    reminder.message,
                )
            except Exception:
                pass


def start_scheduler(db: Session):
    """Start the scheduler and load existing reminders."""
    load_reminders_from_db(db)
    if not scheduler.running:
        scheduler.start()


def shutdown_scheduler():
    if scheduler.running:
        scheduler.shutdown(wait=False)
