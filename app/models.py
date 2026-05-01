from datetime import datetime
from sqlalchemy import Column, Integer, String, Float, DateTime, Boolean, Text, ForeignKey, UniqueConstraint
from sqlalchemy.orm import relationship
from app.database import Base


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    phone_number = Column(String, unique=True, nullable=False, index=True)
    fatsecret_access_token = Column(String, nullable=True)
    fatsecret_access_secret = Column(String, nullable=True)
    nutrichat_api_key = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    meal_entries = relationship("MealEntry", back_populates="user")
    conversation_state = relationship("ConversationState", back_populates="user", uselist=False)
    reminders = relationship("Reminder", back_populates="user")


class MealEntry(Base):
    __tablename__ = "meal_entries"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    meal_type = Column(String, nullable=False)  # breakfast/lunch/dinner/snack
    food_description = Column(Text, nullable=False)
    calories = Column(Float, nullable=True)
    protein_g = Column(Float, nullable=True)
    fat_g = Column(Float, nullable=True)
    carbs_g = Column(Float, nullable=True)
    logged_at = Column(DateTime, default=datetime.utcnow)
    fatsecret_entry_id = Column(String, nullable=True)

    user = relationship("User", back_populates="meal_entries")


class ConversationState(Base):
    __tablename__ = "conversation_states"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, unique=True)
    state = Column(String, default="idle")  # idle / awaiting_meal_type
    pending_data = Column(Text, nullable=True)  # JSON blob
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user = relationship("User", back_populates="conversation_state")


class Reminder(Base):
    __tablename__ = "reminders"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    label = Column(String, nullable=False)
    cron_expression = Column(String, nullable=False)
    message = Column(Text, nullable=False)
    active = Column(Boolean, default=True)

    user = relationship("User", back_populates="reminders")


class SystemConfig(Base):
    """Key-value store for runtime-configurable app settings (e.g. WhatsApp token)."""

    __tablename__ = "system_config"

    key = Column(String, primary_key=True)
    value = Column(Text, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class OAuthTemp(Base):
    """Temporary storage for FatSecret OAuth request token secrets.

    Rows are created at OAuth step 1 and deleted at step 2 (callback).
    Using DB instead of an in-memory dict ensures tokens survive restarts.
    """

    __tablename__ = "oauth_temp"

    id = Column(Integer, primary_key=True, index=True)
    oauth_token = Column(String, unique=True, nullable=False, index=True)
    oauth_token_secret = Column(String, nullable=False)
    phone_number = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
