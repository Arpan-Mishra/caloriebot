import json
import logging
import re
import anthropic
from app.config import get_settings
from app.schemas import NutritionResult, FoodItem

logger = logging.getLogger(__name__)

settings = get_settings()

MEAL_TYPES = {"breakfast", "lunch", "dinner", "snack"}

FOOD_PARSE_PROMPT = """You are a nutrition assistant. The user will describe what they ate.
Extract the following information and respond ONLY with a valid JSON object (no markdown, no extra text):

{{
  "food_description": "<concise description of the full meal>",
  "calories": <total calories as number or null>,
  "protein_g": <total protein in grams as number or null>,
  "fat_g": <total fat in grams as number or null>,
  "carbs_g": <total carbs in grams as number or null>,
  "meal_type": "<breakfast|lunch|dinner|snack or null if not mentioned>",
  "items": [
    {{
      "name": "<ingredient or food item name>",
      "calories": <number or null>,
      "protein_g": <number or null>,
      "fat_g": <number or null>,
      "carbs_g": <number or null>
    }}
  ]
}}

Estimate macros based on typical nutritional values if not explicitly provided.
If you cannot determine a value, use null.
meal_type should only be one of: breakfast, lunch, dinner, snack — or null.
The totals (calories, protein_g, fat_g, carbs_g) should equal the sum of all items.
List every distinct ingredient or food item separately in "items".

User message: {user_message}"""

REMINDER_PARSE_PROMPT = """You are a scheduling assistant. Parse the user's reminder request and respond ONLY with a valid JSON object:

{{
  "label": "<short label for the reminder>",
  "cron_expression": "<standard 5-field cron expression>",
  "message": "<the reminder message to send>"
}}

Examples:
- "remind me at 8pm every day to log dinner" → {{"label": "dinner", "cron_expression": "0 20 * * *", "message": "Time to log your dinner! 🍽️"}}
- "remind me at 9am on weekdays to log breakfast" → {{"label": "breakfast", "cron_expression": "0 9 * * 1-5", "message": "Good morning! Don't forget to log your breakfast 🌅"}}

User message: {user_message}"""

IS_REMINDER_PROMPT = """Does this message contain a request to set a meal logging reminder?
Answer with ONLY "yes" or "no".

Message: {user_message}"""

IS_SUMMARY_PROMPT = """Does this message ask for a daily food/calorie summary or log summary?
Answer with ONLY "yes" or "no".

Message: {user_message}"""


def _get_client() -> anthropic.Anthropic:
    return anthropic.Anthropic(api_key=settings.anthropic_api_key)


def parse_nutrition(user_message: str) -> NutritionResult:
    """Use Claude to extract food items and macros from user message."""
    client = _get_client()
    prompt = FOOD_PARSE_PROMPT.format(user_message=user_message)

    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = message.content[0].text.strip()
    logger.debug("LLM raw response: %s", raw)

    # Strip markdown code fences if present
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)

    data = json.loads(raw)
    # Normalize meal_type
    meal_type = data.get("meal_type")
    if meal_type and meal_type.lower() not in MEAL_TYPES:
        meal_type = None
    elif meal_type:
        meal_type = meal_type.lower()

    items = [
        FoodItem(
            name=item.get("name", ""),
            calories=item.get("calories"),
            protein_g=item.get("protein_g"),
            fat_g=item.get("fat_g"),
            carbs_g=item.get("carbs_g"),
        )
        for item in data.get("items", [])
    ]

    return NutritionResult(
        food_description=data.get("food_description", user_message),
        calories=data.get("calories"),
        protein_g=data.get("protein_g"),
        fat_g=data.get("fat_g"),
        carbs_g=data.get("carbs_g"),
        meal_type=meal_type,
        items=items,
    )


def parse_reminder(user_message: str):
    """Use Claude to parse a reminder request into cron config."""
    from app.schemas import ReminderConfig

    client = _get_client()
    prompt = REMINDER_PARSE_PROMPT.format(user_message=user_message)

    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=256,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = message.content[0].text.strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)

    data = json.loads(raw)
    return ReminderConfig(
        label=data["label"],
        cron_expression=data["cron_expression"],
        message=data["message"],
    )


def is_reminder_request(user_message: str) -> bool:
    """Detect if the user is asking to set a reminder."""
    client = _get_client()
    prompt = IS_REMINDER_PROMPT.format(user_message=user_message)
    message = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=10,
        messages=[{"role": "user", "content": prompt}],
    )
    return message.content[0].text.strip().lower() == "yes"


def is_summary_request(user_message: str) -> bool:
    """Detect if the user is asking for a daily summary."""
    client = _get_client()
    prompt = IS_SUMMARY_PROMPT.format(user_message=user_message)
    message = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=10,
        messages=[{"role": "user", "content": prompt}],
    )
    return message.content[0].text.strip().lower() == "yes"
