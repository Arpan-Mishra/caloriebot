"""
NutriChat API integration via the nutrichat Python SDK.

Wraps the async NutriChat client and adapts responses to match the dict format
used by FatSecret (calories_per_serving, serving_id, etc.) so the nutrition
agent prompt and tool schemas require minimal changes.
"""
import logging
from nutrichat import NutriChatClient, AuthError, NutriChatError, RateLimitError
from app.config import get_settings

settings = get_settings()
logger = logging.getLogger(__name__)


def _get_client(api_key: str) -> NutriChatClient:
    """Return a NutriChatClient for the given user's API key."""
    return NutriChatClient(
        api_key=api_key,
        base_url=settings.nutrichat_base_url,
    )


def _adapt_search_result(item: dict) -> dict:
    """Convert NutriChat search result dict to FatSecret-compatible format.

    NutriChat returns keys like ``calories``, ``protein_g`` (per serving).
    The agent prompt and tool schemas expect ``calories_per_serving``,
    ``protein_g_per_serving``, ``serving_id``, and ``serving_number_of_units``.
    """
    return {
        "food_id": str(item.get("food_id", "")),
        "food_name": item.get("food_name", ""),
        "serving_id": str(item.get("food_id", "")),  # NutriChat has no separate serving_id
        "serving_description": item.get("serving_description", "1 serving"),
        "metric_serving_amount": float(item.get("metric_serving_amount") or 0),
        "metric_serving_unit": item.get("metric_serving_unit", "g"),
        "serving_number_of_units": 1.0,  # NutriChat uses direct units, no FatSecret scaling
        "calories_per_serving": float(item.get("calories") or 0),
        "protein_g_per_serving": float(item.get("protein_g") or 0),
        "fat_g_per_serving": float(item.get("fat_g") or 0),
        "carbs_g_per_serving": float(item.get("carbs_g") or 0),
        "match_score": float(item.get("match_score") or 0),
    }


async def search_food(query: str, api_key: str) -> list[dict]:
    """Search NutriChat for food items.

    Returns FatSecret-compatible dicts so the agent doesn't need prompt changes.
    """
    try:
        async with _get_client(api_key) as client:
            results = await client.search_food(query, limit=5)
        adapted = [_adapt_search_result(r) for r in results]
        logger.info("search_food %r → %d results", query, len(adapted))
        return adapted
    except AuthError:
        logger.error("NutriChat auth failed for search_food %r — API key may be revoked", query)
        return []
    except RateLimitError:
        logger.warning("NutriChat rate limited on search_food %r", query)
        return []
    except NutriChatError:
        logger.exception("NutriChat search_food failed for %r", query)
        return []


async def log_food_entries_batch(
    items: list[dict],
    meal_type: str,
    api_key: str,
) -> list[dict]:
    """Log multiple food entries via NutriChat API.

    Accepts the same item shape as the FatSecret function (food_id, number_of_units, etc.).
    Returns list of dicts with: entry_id, food_name, calories, protein_g, fat_g, carbs_g.
    """
    # Map meal_type: FatSecret uses "other" for snacks, NutriChat uses "snack"
    nc_meal_type = "snack" if meal_type == "other" else meal_type

    # Convert items to NutriChat format
    nc_items = []
    for item in items:
        nc_item = {
            "food_id": int(item["food_id"]),
            "food_name": item.get("food_name", ""),
            "number_of_units": float(item.get("number_of_units") or 1),
            "calories": float(item.get("calories") or 0),
            "protein_g": float(item.get("protein_g") or 0),
            "fat_g": float(item.get("fat_g") or 0),
            "carbs_g": float(item.get("carbs_g") or 0),
        }
        # Only pass metric_serving_amount if present and non-zero;
        # the SDK defaults to 100 when omitted
        msa = float(item.get("metric_serving_amount") or 0)
        if msa > 0:
            nc_item["metric_serving_amount"] = msa
        nc_items.append(nc_item)

    try:
        async with _get_client(api_key) as client:
            results = await client.log_food_entries_batch(nc_items, meal_type=nc_meal_type)
        logger.info("log_food_entries_batch: %d items logged via NutriChat", len(results))

        # Normalize response to match what the agent expects
        out = []
        for r in results:
            out.append({
                "entry_id": str(r.get("id", "")),
                "food_name": r.get("food_description", r.get("food_name", "")),
                "calories": float(r.get("calories") or 0),
                "protein_g": float(r.get("protein_g") or 0),
                "fat_g": float(r.get("fat_g") or 0),
                "carbs_g": float(r.get("carbs_g") or 0),
            })
        return out
    except AuthError:
        logger.error("NutriChat auth failed on log_food_entries_batch — API key may be revoked")
        return []
    except NutriChatError:
        logger.exception("NutriChat log_food_entries_batch failed")
        return []


async def get_food_entries_today(api_key: str) -> dict:
    """Get today's food diary totals from NutriChat.

    Returns dict with: calories, protein_g, fat_g, carbs_g, meal_count.
    """
    try:
        async with _get_client(api_key) as client:
            totals = await client.get_today_totals()
        logger.info(
            "NutriChat today: cal=%.0f pro=%.1f fat=%.1f carb=%.1f",
            totals.get("calories", 0),
            totals.get("protein_g", 0),
            totals.get("fat_g", 0),
            totals.get("carbs_g", 0),
        )
        return {
            "calories": totals.get("calories", 0),
            "protein_g": totals.get("protein_g", 0),
            "fat_g": totals.get("fat_g", 0),
            "carbs_g": totals.get("carbs_g", 0),
            "meal_count": len(totals.get("meals", [])),
        }
    except AuthError:
        logger.error("NutriChat auth failed on get_food_entries_today — API key may be revoked")
        return {"calories": 0, "protein_g": 0, "fat_g": 0, "carbs_g": 0, "meal_count": 0}
    except NutriChatError:
        logger.exception("NutriChat get_food_entries_today failed")
        return {"calories": 0, "protein_g": 0, "fat_g": 0, "carbs_g": 0, "meal_count": 0}
