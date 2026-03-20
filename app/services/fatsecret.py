"""
FatSecret Platform API integration via pyfatsecret.

Each user authenticates via OAuth 1.0a. After the OAuth flow the access
token + secret are stored in the User row so subsequent calls can act on
their behalf.

Reference: https://platform.fatsecret.com/api/
"""
import logging
import re
from datetime import datetime
from typing import Optional
from fatsecret import Fatsecret
from app.config import get_settings

settings = get_settings()
logger = logging.getLogger(__name__)

# FatSecret accepts: "breakfast", "lunch", "dinner", "other"
MEAL_TYPE_MAP = {
    "breakfast": "breakfast",
    "lunch": "lunch",
    "dinner": "dinner",
    "snack": "other",
}


def _get_client(access_token: Optional[str] = None, access_secret: Optional[str] = None) -> Fatsecret:
    """Return a Fatsecret client, optionally authenticated as a specific user."""
    session_token = (access_token, access_secret) if access_token and access_secret else None
    return Fatsecret(
        settings.fatsecret_consumer_key,
        settings.fatsecret_consumer_secret,
        session_token=session_token,
    )


def get_request_token(callback_url: str = "oob") -> tuple[str, str, str]:
    """
    Step 1 of OAuth: get request token + auth URL.
    Returns (oauth_token, oauth_token_secret, auth_url).
    Token/secret are stored on the client after get_authorize_url() is called.
    """
    client = _get_client()
    auth_url = client.get_authorize_url(callback_url=callback_url)
    return client.request_token, client.request_token_secret, auth_url


def get_access_token(oauth_token: str, oauth_token_secret: str, oauth_verifier: str) -> tuple[str, str]:
    """
    Step 2 of OAuth: exchange verifier for access token.
    Returns (access_token, access_secret).
    """
    client = _get_client()
    # Restore the request token state so authenticate() can use it
    client.request_token = oauth_token
    client.request_token_secret = oauth_token_secret
    access_token, access_secret = client.authenticate(oauth_verifier)
    return access_token, access_secret


def log_food_entry(
    food_description: str,
    meal_type: str,
    access_token: str,
    access_secret: str,
) -> Optional[dict]:
    """
    Search FatSecret for a food item, log the entry, and return nutrition info.

    Returns a dict with keys: entry_id, food_name, calories, protein_g, fat_g, carbs_g.
    Returns None on failure (no results, API error, etc.).
    """
    client = _get_client(access_token, access_secret)
    meal_str = MEAL_TYPE_MAP.get(meal_type.lower(), "other")

    try:
        logger.debug("FatSecret search query: %r", food_description)
        results = client.foods_search(food_description)
        if not results:
            logger.warning("FatSecret: no results for %r", food_description)
            return None

        food = results[0]
        food_id = food["food_id"]
        food_name = food.get("food_name", food_description)
        logger.debug("FatSecret search result: food_id=%s food_name=%r", food_id, food_name)

        food_detail = client.food_get(food_id)
        servings = food_detail.get("servings", {}).get("serving", [])
        if isinstance(servings, dict):
            servings = [servings]

        if not servings:
            return None
        serving = servings[0]
        serving_id = serving.get("serving_id")
        if not serving_id:
            return None

        entry = client.food_entry_create(
            food_id=food_id,
            food_entry_name=food_name,
            serving_id=serving_id,
            number_of_units=1,
            meal=meal_str,
        )
        entry_id = str(entry.get("value") or entry.get("food_entry_id", ""))
        logger.debug("FatSecret entry created: id=%s food=%r meal=%s", entry_id, food_name, meal_str)

        return {
            "entry_id": entry_id,
            "food_name": food_name,
            "calories": float(serving.get("calories") or 0),
            "protein_g": float(serving.get("protein") or 0),
            "fat_g": float(serving.get("fat") or 0),
            "carbs_g": float(serving.get("carbohydrate") or 0),
        }
    except Exception:
        logger.exception("FatSecret log_food_entry failed for %r", food_description)
        return None


def _similarity_score(query: str, food_name: str) -> float:
    """Score how well a FatSecret food_name matches the search query.

    Uses word-level overlap with a bonus for brand names (words in parentheses).
    Returns a float in [0, 1] — higher is better.
    """
    _noise = {"g", "ml", "oz", "kg", "lb", "cup", "cups", "tbsp", "tsp",
              "serving", "servings", "the", "and", "or", "of", "in", "a",
              "with", "per", "for", "by"}

    def _tokenize(text: str) -> set[str]:
        return {w for w in re.split(r"[\s,\(\)\/\-]+", text.lower()) if w and w not in _noise}

    # Extract brand tokens from parentheses in food_name, e.g. "(Pintola)"
    brand_tokens = {w.lower() for w in re.findall(r"\(([^)]+)\)", food_name)}

    q_tokens = _tokenize(query)
    r_tokens = _tokenize(food_name)

    if not q_tokens:
        return 0.0

    overlap = q_tokens & r_tokens
    base_score = len(overlap) / len(q_tokens)

    # Bonus when a query word matches a brand name buried in parentheses
    brand_overlap = q_tokens & brand_tokens
    brand_bonus = 0.2 * len(brand_overlap) / len(q_tokens) if brand_overlap else 0.0

    return min(base_score + brand_bonus, 1.0)


def _autocomplete_query(client: Fatsecret, query: str) -> str:
    """Map a colloquial food query to FatSecret's canonical name via autocomplete.

    foods_autocomplete returns ranked name suggestions from FatSecret's database
    (e.g. "pintola protein oats" → "High Protein Oats (Pintola)"). We pick the
    suggestion most similar to the original query and use it as the search term,
    dramatically improving match quality for brand names and regional foods.

    Falls back to the original query on any error or empty result.
    """
    try:
        raw = client.foods_autocomplete(query, max_results=30)
        suggestions: list[str] = raw.get("suggestion", []) if isinstance(raw, dict) else []
        if not suggestions:
            return query
        best = max(suggestions, key=lambda s: _similarity_score(query, s))
        logger.debug(
            "Autocomplete %r → %r (score=%.2f, %d suggestions)",
            query, best, _similarity_score(query, best), len(suggestions),
        )
        return best
    except Exception:
        logger.warning("foods_autocomplete failed for %r, falling back to original query", query)
        return query


# Number of top candidates to fetch full serving details for.
# foods_search returns up to 30 names cheaply; food_get is one call per item.
# Fetching only the top-scored candidates avoids FatSecret rate limiting.
_FOOD_GET_LIMIT = 5


def search_food(
    query: str,
    access_token: str,
    access_secret: str,
) -> list[dict]:
    """Search FatSecret for a food item; return up to 5 matches sorted by similarity.

    Strategy (minimises API calls to avoid rate limiting):
      1. foods_autocomplete  → 1 call: map colloquial name to FatSecret canonical name
      2. foods_search        → 1 call: fetch up to 30 food names + IDs (no macros yet)
      3. Similarity scoring  → 0 calls: rank all 30 by name match against original query
      4. food_get × 5        → 5 calls: fetch full serving details only for top candidates

    Total: ~7 API calls instead of 31.

    Each result includes: food_id, food_name, serving_id, serving_description,
    metric_serving_amount, metric_serving_unit, calories_per_serving,
    protein_g_per_serving, fat_g_per_serving, carbs_g_per_serving, match_score.
    Returns an empty list on failure or no results.
    """
    client = _get_client(access_token, access_secret)
    try:
        # Step 1: autocomplete → canonical FatSecret name
        effective_query = _autocomplete_query(client, query)
        logger.debug("FatSecret search_food: original=%r effective=%r", query, effective_query)

        # Step 2: cheap name-only search (no food_get yet)
        candidates = client.foods_search(effective_query, max_results=30)
        if not candidates and effective_query != query:
            logger.warning(
                "No results for autocomplete query %r, retrying with original %r",
                effective_query, query,
            )
            candidates = client.foods_search(query, max_results=30)
        if not candidates:
            logger.warning("FatSecret: no results for %r", query)
            return []
        if isinstance(candidates, dict):
            candidates = [candidates]

        # Step 3: score all candidates by name similarity — zero extra API calls
        scored = sorted(
            candidates,
            key=lambda f: _similarity_score(query, f.get("food_name", "")),
            reverse=True,
        )
        top_candidates = scored[:_FOOD_GET_LIMIT]
        logger.debug(
            "search_food %r: top %d of %d — %s",
            query, len(top_candidates), len(scored),
            [(f["food_name"], round(_similarity_score(query, f["food_name"]), 2))
             for f in top_candidates],
        )

        # Step 4: food_get only for the top candidates
        output = []
        for food in top_candidates:
            food_id = food["food_id"]
            food_name = food.get("food_name", query)
            try:
                food_detail = client.food_get(food_id)
                servings = food_detail.get("servings", {}).get("serving", [])
                if isinstance(servings, dict):
                    servings = [servings]
                if not servings:
                    continue
                serving = servings[0]
                serving_id = serving.get("serving_id")
                if not serving_id:
                    continue
                output.append({
                    "food_id": str(food_id),
                    "food_name": food_name,
                    "serving_id": str(serving_id),
                    "serving_description": serving.get("serving_description", "1 serving"),
                    "metric_serving_amount": float(serving.get("metric_serving_amount") or 0),
                    "metric_serving_unit": serving.get("metric_serving_unit", "g"),
                    # FatSecret's own number_of_units for this serving — needed at log time.
                    # e.g. a "100g" serving has serving_number_of_units=100 (measurement_description="g").
                    # food_entry_create expects: agent_servings × serving_number_of_units.
                    "serving_number_of_units": float(serving.get("number_of_units") or 1),
                    "calories_per_serving": float(serving.get("calories") or 0),
                    "protein_g_per_serving": float(serving.get("protein") or 0),
                    "fat_g_per_serving": float(serving.get("fat") or 0),
                    "carbs_g_per_serving": float(serving.get("carbohydrate") or 0),
                    "match_score": round(_similarity_score(query, food_name), 3),
                })
            except Exception:
                logger.exception("FatSecret food_get failed for food_id=%s food_name=%r", food_id, food_name)
                continue

        output.sort(key=lambda r: r["match_score"], reverse=True)
        logger.info(
            "search_food %r → %d results, best=%r score=%.2f",
            query,
            len(output),
            output[0]["food_name"] if output else None,
            output[0]["match_score"] if output else 0,
        )
        return output
    except Exception:
        logger.exception("FatSecret search_food failed for %r", query)
        return []


def log_food_entries_batch(
    items: list[dict],
    meal_type: str,
    access_token: str,
    access_secret: str,
) -> list[dict]:
    """Log multiple food entries to FatSecret diary.

    Each item must contain: food_id, food_name, serving_id, and macro fields
    (calories, protein_g, fat_g, carbs_g) to be included in the return value.

    Returns a list of dicts with keys: entry_id, food_name, calories,
    protein_g, fat_g, carbs_g.
    """
    client = _get_client(access_token, access_secret)
    meal_str = MEAL_TYPE_MAP.get(meal_type.lower(), "other")

    results = []
    for item in items:
        food_id = item.get("food_id")
        food_name = item.get("food_name", "Unknown")
        serving_id = item.get("serving_id")

        if not food_id or not serving_id:
            logger.warning("Skipping item missing food_id or serving_id: %r", food_name)
            continue

        # agent_servings: how many servings the agent calculated (e.g. 2.0 for 200g of a 100g serving)
        # serving_number_of_units: FatSecret's own unit scale for this serving
        #   (e.g. 100 when measurement_description="g" and the serving is 100g)
        # food_entry_create expects the raw measurement units, not the number of servings.
        # Example: 200g chicken, serving_number_of_units=100 → pass 2.0 × 100 = 200 (grams).
        agent_servings = float(item.get("number_of_units") or 1)
        serving_number_of_units = float(item.get("serving_number_of_units") or 1)
        fatsecret_units = agent_servings * serving_number_of_units
        try:
            entry = client.food_entry_create(
                food_id=food_id,
                food_entry_name=food_name,
                serving_id=serving_id,
                number_of_units=fatsecret_units,
                meal=meal_str,
            )
            entry_id = str(entry.get("value") or entry.get("food_entry_id", ""))
            logger.debug(
                "FatSecret entry created: id=%s food=%r agent_servings=%.2f "
                "serving_units=%.1f fatsecret_units=%.2f meal=%s",
                entry_id, food_name, agent_servings, serving_number_of_units,
                fatsecret_units, meal_str,
            )
            results.append({
                "entry_id": entry_id,
                "food_name": food_name,
                "calories": float(item.get("calories") or 0),
                "protein_g": float(item.get("protein_g") or 0),
                "fat_g": float(item.get("fat_g") or 0),
                "carbs_g": float(item.get("carbs_g") or 0),
            })
        except Exception:
            logger.exception("FatSecret food_entry_create failed for %r", food_name)

    return results


def get_food_entries_today(access_token: str, access_secret: str) -> dict:
    """Fetch today's food diary entries from FatSecret and return summed totals.

    FatSecret is the authoritative source for what was actually logged.
    Returns a dict with keys: calories, protein_g, fat_g, carbs_g, meal_count.
    Returns zeroed totals on failure so the agent can still format a reply.
    """
    client = _get_client(access_token, access_secret)
    today = datetime.today()
    try:
        response = client.food_entries_get(date=today)
        logger.debug("FatSecret food_entries_get date=%s raw=%s", today.date(), response)

        # valid_response already unwraps food_entries → returns a list of entry dicts
        # (or [] when the day is empty, or None if the API call itself returned nothing)
        if not response:
            logger.info("FatSecret: no diary entries found for %s", today.date())
            return {"calories": 0, "protein_g": 0, "fat_g": 0, "carbs_g": 0, "meal_count": 0}

        entries: list[dict] = response if isinstance(response, list) else []

        total_cal = sum(float(e.get("calories") or 0) for e in entries)
        total_pro = sum(float(e.get("protein") or 0) for e in entries)
        total_fat = sum(float(e.get("fat") or 0) for e in entries)
        total_carb = sum(float(e.get("carbohydrate") or 0) for e in entries)

        logger.info(
            "FatSecret today (%s): %d entries — cal=%.0f pro=%.1f fat=%.1f carb=%.1f",
            today.date(), len(entries), total_cal, total_pro, total_fat, total_carb,
        )
        return {
            "calories": total_cal,
            "protein_g": total_pro,
            "fat_g": total_fat,
            "carbs_g": total_carb,
            "meal_count": len(entries),
        }
    except Exception:
        logger.exception("FatSecret get_food_entries_today failed for date=%s", today.date())
        return {"calories": 0, "protein_g": 0, "fat_g": 0, "carbs_g": 0, "meal_count": 0}
