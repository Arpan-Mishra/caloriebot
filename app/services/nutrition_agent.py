"""
Agentic nutrition pipeline built with LangGraph + Claude (langchain-anthropic).

Graph topology
--------------
  START ──► agent ──► tools ──► agent ──► … ──► END
                 └─────────────────────────────────►

The agent node calls Claude Sonnet with tools bound. When the model returns
tool calls the custom tools node executes all of them concurrently, updates
the graph state, and loops back to the agent. When the model returns a plain
text response the graph terminates and that text is the WhatsApp reply.

State design
------------
``NutritionAgentState`` is the single source of truth for the entire agent
session. Fields are documented below and designed so that adding a LangGraph
checkpointer in the future (e.g. ``SqliteSaver``) will automatically give the
agent persistent memory across conversations with no code changes beyond
passing a ``config={"configurable": {"thread_id": user.id}}`` to ``ainvoke``.

  messages            – Append-only conversation history (LangGraph add_messages
                        reducer). Includes the system prompt, user turn, all
                        AIMessages (with or without tool_calls), and ToolMessages.
                        This is the primary memory surface for future LLM context.

  meal_type           – Inferred from time of day at invocation; never mutated
                        by the graph. Stored so a checkpointer can reconstruct
                        which meal a past session belonged to.

  has_fatsecret       – Snapshot of whether FatSecret tokens were present at
                        invocation. Stored so replays / memory reads reflect
                        the context under which logging happened.

  food_description    – Populated by the log_food_entries tool. Short human-
                        readable description of the full meal that was logged
                        (e.g. "Dal rice with salad"). Designed as a memory
                        anchor: future sessions can recall "last Tuesday you
                        had dal rice for lunch" from this field.

  logged_items        – Populated by the log_food_entries tool. List of dicts
                        with food_name, calories, protein_g, fat_g, carbs_g for
                        every item that was confirmed (FatSecret or estimated).
                        Designed as a memory anchor for per-item recall.

  fatsecret_entry_ids – Populated by the log_food_entries tool. FatSecret diary
                        entry IDs as strings. Lets future memory/audit code
                        cross-reference local DB records with FatSecret entries.
"""

import asyncio
import json
import logging
from datetime import date, datetime
from typing import Annotated, Any, Optional
from typing_extensions import TypedDict

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import MealEntry, User
from app.services import fatsecret as fs_svc

logger = logging.getLogger(__name__)
settings = get_settings()


# ---------------------------------------------------------------------------
# Agent system prompt
# ---------------------------------------------------------------------------

AGENT_SYSTEM_PROMPT = """You are a nutrition logging assistant for a WhatsApp calorie tracking bot.

Work through the following phases in order. Do your thinking explicitly before making tool calls.

---

## PHASE 1 — Parse the user message (no tools)

Read the user message and extract a structured list. For each food item note:
- **item_name**: clean food name, no quantities (e.g. "pintola protein oats")
- **user_quantity**: the amount the user mentioned (e.g. "50g", "2 cups", "1 bowl") or null if not stated
- **search_query**: same as item_name — stripped of all quantities and units

Example: "I had pintola protein oats 50g and 2 boiled eggs"
→ [{item_name: "pintola protein oats", user_quantity: "50g", search_query: "pintola protein oats"},
   {item_name: "boiled eggs", user_quantity: "2", search_query: "boiled egg"}]

---

## PHASE 2 — Search FatSecret (parallel, fatsecret_connected = true only)

Call search_food for EVERY item IN PARALLEL (all in one response turn).
Use the search_query from Phase 1 — never include quantities.

Each result includes:
- match_score (0–1): pre-computed similarity — higher is better
- metric_serving_amount + metric_serving_unit: the numeric serving size (e.g. 30.0, "g")
- calories_per_serving, protein_g_per_serving, fat_g_per_serving, carbs_g_per_serving

If the top result has match_score < 0.3, retry with a shorter query (e.g. "protein oats" instead of "pintola protein oats"). Up to 2 retries per item.

---

## PHASE 3 — Select best match and calculate number_of_units

For each item, after seeing search results:

**3a. Pick the best match**
Choose the result with the highest match_score whose name makes sense for the described food.

**3b. Calculate number_of_units — follow these rules exactly**

The formula is always:
  number_of_units = user_amount_in_metric_unit / metric_serving_amount

Work through each case:

| User said | Serving (metric_serving_amount / unit) | Calculation | number_of_units |
|-----------|----------------------------------------|-------------|-----------------|
| 200g      | 100 g                                  | 200 / 100   | 2.0             |
| 50g       | 30 g                                   | 50 / 30     | 1.67            |
| 100ml     | 250 ml                                 | 100 / 250   | 0.4             |
| 2 eggs    | 1 (serving is "1 egg")                 | 2 / 1       | 2.0             |
| 1 egg     | 1 (serving is "1 egg")                 | 1 / 1       | 1.0             |
| 3 chapati | 1 (serving is "1 piece")               | 3 / 1       | 3.0             |

**Unit mismatch — convert first, then divide:**
- If serving is in grams and user said a volume/count with a known gram equivalent, convert first.
  → "1 cup oats": 1 cup ≈ 80g. Serving is 30g. → number_of_units = 80 / 30 = 2.67
  → "1 tbsp peanut butter": 1 tbsp ≈ 16g. Serving is 32g. → number_of_units = 0.5

**Vague quantities — approximate using common sense:**
- "1 bowl" rice/dal/oats ≈ 150–200g cooked (use 175g as default)
- "1 plate" rice/curry ≈ 200–250g
- "1 scoop" protein powder → check serving_description; if it says "1 scoop" use 1.0
- "1 glass" milk/juice ≈ 200ml

**No quantity stated at all:**
- Use number_of_units = 1.0 (one standard serving as listed in FatSecret)

**Sanity check — before passing to log_food_entries:**
- number_of_units should almost always be between 0.1 and 20
- If your calculation gives a number outside this range, recheck your math
- A 200g portion of any food should NEVER produce number_of_units < 0.5 or > 10

**3c. Scale macros:**
  calories  = calories_per_serving  × number_of_units
  protein_g = protein_g_per_serving × number_of_units
  fat_g     = fat_g_per_serving     × number_of_units
  carbs_g   = carbs_g_per_serving   × number_of_units

**3d. Carry serving_number_of_units forward:**
Copy the `serving_number_of_units` value from the search result directly into the log_food_entries item.
Do not modify it — the backend needs it to correctly convert your servings count into FatSecret's native unit.

---

## PHASE 4 — Log

Call log_food_entries ONCE with all finalised items.
Pass: food_id, serving_id, number_of_units (calculated above), and the SCALED macro values.

---

## PHASE 5 — Get daily total

Call get_today_totals. The result INCLUDES the meal just logged.
Use it directly as "Today's total" — do NOT add the current meal on top.

---

## PHASE 6 — Reply

Use the log_food_entries response for 📊. Use get_today_totals for 📅.

✅ Logged *{Meal Type}*: {food description}

📊 *Nutrition logged:*
  • Calories: {X} kcal
  • Protein:  {X.X} g
  • Carbs:    {X.X} g
  • Fat:      {X.X} g

🔍 *Breakdown:*
  • {FatSecret food name} ({number_of_units × metric_serving_amount}{unit}, e.g. "200g"): {cal} kcal | {pro}p / {carb}c / {fat}f

📅 *Today's total:* {X} kcal | {pro}p / {carb}c / {fat}f

---

## If fatsecret_connected is false

Skip Phases 2–3. Estimate macros from your knowledge, apply any quantity scaling, then call log_food_entries with your estimates (omit food_id/serving_id, set number_of_units=1.0)."""


# ---------------------------------------------------------------------------
# Tool schemas (Anthropic format — accepted by ChatAnthropic.bind_tools)
# ---------------------------------------------------------------------------

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "search_food",
        "description": (
            "Search FatSecret for a food item. Returns up to 5 matches sorted by name similarity. "
            "Each result includes: food_id, food_name, serving_id, serving_description, "
            "metric_serving_amount (numeric), metric_serving_unit (g/ml/oz), "
            "calories_per_serving, protein_g_per_serving, fat_g_per_serving, carbs_g_per_serving, "
            "and match_score (0–1, higher = better match). "
            "Use metric_serving_amount and metric_serving_unit to calculate number_of_units. "
            "Query must be food name only — no quantities or units."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "Food name only — no quantities or units. "
                        "e.g. 'chicken breast', 'banana', 'pintola protein oats'"
                    ),
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "log_food_entries",
        "description": (
            "Log selected food entries to the diary. Call once after searching, "
            "with all items. Saves to FatSecret (if connected) and local database."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "food_description": {
                    "type": "string",
                    "description": "Short description of the full meal, e.g. 'Dal rice with salad'",
                },
                "items": {
                    "type": "array",
                    "description": "Food items to log",
                    "items": {
                        "type": "object",
                        "properties": {
                            "food_id": {
                                "type": "string",
                                "description": "FatSecret food_id (omit when estimating)",
                            },
                            "serving_id": {
                                "type": "string",
                                "description": "FatSecret serving_id (omit when estimating)",
                            },
                            "serving_number_of_units": {
                                "type": "number",
                                "description": (
                                    "Copy the serving_number_of_units value from the search_food result unchanged. "
                                    "The backend uses this to convert agent servings → FatSecret's native unit. "
                                    "Omit when estimating (no FatSecret match)."
                                ),
                            },
                            "number_of_units": {
                                "type": "number",
                                "description": (
                                    "Number of servings to log, calculated as: "
                                    "user_quantity_in_metric_unit / metric_serving_amount. "
                                    "E.g. user said 200g, metric_serving_amount=100g → 2.0. "
                                    "E.g. user said 2 eggs, serving is '1 egg' → 2.0. "
                                    "Defaults to 1.0 if no quantity stated."
                                ),
                            },
                            "food_name": {"type": "string"},
                            "calories": {
                                "type": "number",
                                "description": "Calories scaled to the actual quantity logged (calories_per_serving × number_of_units)",
                            },
                            "protein_g": {
                                "type": "number",
                                "description": "Protein scaled to the actual quantity logged",
                            },
                            "fat_g": {
                                "type": "number",
                                "description": "Fat scaled to the actual quantity logged",
                            },
                            "carbs_g": {
                                "type": "number",
                                "description": "Carbs scaled to the actual quantity logged",
                            },
                        },
                        "required": ["food_name", "calories", "protein_g", "fat_g", "carbs_g"],
                    },
                },
            },
            "required": ["food_description", "items"],
        },
    },
    {
        "name": "get_today_totals",
        "description": (
            "Get the user's total calories and macros for today, INCLUDING the meal "
            "that was just logged. Call this after log_food_entries. Use the returned "
            "value directly as 'Today's total' — do NOT add the current meal on top of it."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_meal_type",
        "description": "Get the current meal type inferred from time of day.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
]


# ---------------------------------------------------------------------------
# State schema
# ---------------------------------------------------------------------------

class NutritionAgentState(TypedDict):
    """LangGraph state for one nutrition-logging agent session.

    See module docstring for detailed field documentation.
    """
    messages: Annotated[list, add_messages]
    meal_type: str
    has_fatsecret: bool
    # Populated by log_food_entries tool — designed as future memory anchors
    food_description: str
    logged_items: list
    fatsecret_entry_ids: list


# ---------------------------------------------------------------------------
# Graph builder
# ---------------------------------------------------------------------------

def _build_graph(user: User, meal_type: str, db: Session):
    """Build and compile the LangGraph for one agent invocation.

    Tool handlers are closures that capture ``user``, ``meal_type``, and ``db``
    at build time. A fresh graph is compiled per request — no shared mutable
    state between concurrent WhatsApp messages.

    To add persistent memory in the future, pass a checkpointer to
    ``graph.compile(checkpointer=…)`` and supply
    ``config={"configurable": {"thread_id": str(user.id)}}`` to ``ainvoke``.
    """
    has_fatsecret = bool(user.fatsecret_access_token and user.fatsecret_access_secret)

    # Accumulates state patches from tools so the tools node can return them
    # as proper graph state updates. Only log_food_entries ever appends here.
    _tool_state_patches: list[dict] = []

    # -----------------------------------------------------------------
    # Tool handler coroutines (closures)
    # -----------------------------------------------------------------

    async def _handle_search_food(args: dict) -> str:
        query: str = args["query"]
        if not has_fatsecret:
            return json.dumps({"error": "FatSecret not connected", "results": []})
        results = await asyncio.to_thread(
            fs_svc.search_food,
            query,
            user.fatsecret_access_token,
            user.fatsecret_access_secret,
        )
        logger.debug("search_food %r → %d results", query, len(results))
        return json.dumps({"results": results})

    async def _handle_log_food_entries(args: dict) -> str:
        food_description: str = args["food_description"]
        items: list[dict] = args["items"]

        fs_ids: list[str] = []
        confirmed_items: list[dict] = []

        if has_fatsecret:
            fs_items = [i for i in items if i.get("food_id") and i.get("serving_id")]
            estimate_items = [i for i in items if not (i.get("food_id") and i.get("serving_id"))]

            if fs_items:
                fs_results = await asyncio.to_thread(
                    fs_svc.log_food_entries_batch,
                    fs_items,
                    meal_type,
                    user.fatsecret_access_token,
                    user.fatsecret_access_secret,
                )
                for r in fs_results:
                    fs_ids.append(r["entry_id"])
                    confirmed_items.append(r)

            for item in estimate_items:
                confirmed_items.append({
                    "food_name": item["food_name"],
                    "calories": float(item.get("calories") or 0),
                    "protein_g": float(item.get("protein_g") or 0),
                    "fat_g": float(item.get("fat_g") or 0),
                    "carbs_g": float(item.get("carbs_g") or 0),
                })
        else:
            for item in items:
                confirmed_items.append({
                    "food_name": item["food_name"],
                    "calories": float(item.get("calories") or 0),
                    "protein_g": float(item.get("protein_g") or 0),
                    "fat_g": float(item.get("fat_g") or 0),
                    "carbs_g": float(item.get("carbs_g") or 0),
                })

        total_cal = sum(i["calories"] for i in confirmed_items)
        total_pro = sum(i["protein_g"] for i in confirmed_items)
        total_fat = sum(i["fat_g"] for i in confirmed_items)
        total_carb = sum(i["carbs_g"] for i in confirmed_items)

        entry = MealEntry(
            user_id=user.id,
            meal_type=meal_type,
            food_description=food_description,
            calories=total_cal,
            protein_g=total_pro,
            fat_g=total_fat,
            carbs_g=total_carb,
            logged_at=datetime.utcnow(),
            fatsecret_entry_id=",".join(fs_ids) if fs_ids else None,
        )
        db.add(entry)
        db.commit()
        db.refresh(entry)

        logger.info(
            "Logged meal entry id=%d: %r cal=%.0f pro=%.1f fat=%.1f carb=%.1f",
            entry.id, food_description, total_cal, total_pro, total_fat, total_carb,
        )

        # Queue state update so tools_node can propagate it to graph state
        _tool_state_patches.append({
            "food_description": food_description,
            "logged_items": confirmed_items,
            "fatsecret_entry_ids": fs_ids,
        })

        return json.dumps({
            "logged": True,
            "db_entry_id": entry.id,
            "items": confirmed_items,
            "totals": {
                "calories": total_cal,
                "protein_g": total_pro,
                "fat_g": total_fat,
                "carbs_g": total_carb,
            },
        })

    async def _handle_get_today_totals(_args: dict) -> str:
        if has_fatsecret:
            # FatSecret is the source of truth — fetch what's actually in the diary
            totals = await asyncio.to_thread(
                fs_svc.get_food_entries_today,
                user.fatsecret_access_token,
                user.fatsecret_access_secret,
            )
        else:
            # Fallback: sum today's local DB entries (estimated macros only)
            today = date.today()
            entries = (
                db.query(MealEntry)
                .filter(
                    MealEntry.user_id == user.id,
                    MealEntry.logged_at >= datetime(today.year, today.month, today.day),
                )
                .all()
            )
            totals = {
                "calories": sum(e.calories or 0 for e in entries),
                "protein_g": sum(e.protein_g or 0 for e in entries),
                "fat_g": sum(e.fat_g or 0 for e in entries),
                "carbs_g": sum(e.carbs_g or 0 for e in entries),
                "meal_count": len(entries),
            }
        return json.dumps(totals)

    async def _handle_get_meal_type(_args: dict) -> str:
        return json.dumps({"meal_type": meal_type})

    _tool_dispatch: dict[str, Any] = {
        "search_food": _handle_search_food,
        "log_food_entries": _handle_log_food_entries,
        "get_today_totals": _handle_get_today_totals,
        "get_meal_type": _handle_get_meal_type,
    }

    # -----------------------------------------------------------------
    # Graph nodes
    # -----------------------------------------------------------------

    llm = ChatAnthropic(
        model="claude-sonnet-4-6",
        api_key=settings.anthropic_api_key,
    ).bind_tools(TOOL_SCHEMAS)

    async def agent_node(state: NutritionAgentState) -> dict:
        """Call the LLM with the current message history."""
        response = await llm.ainvoke(state["messages"])
        logger.debug(
            "Agent node: stop_reason=%s tool_calls=%d",
            getattr(response, "stop_reason", "?"),
            len(getattr(response, "tool_calls", []) or []),
        )
        return {"messages": [response]}

    async def tools_node(state: NutritionAgentState) -> dict:
        """Execute all tool calls from the last AIMessage concurrently.

        All tool calls returned by a single agent node response run in parallel
        via asyncio.gather. State patches (from log_food_entries) are merged
        and returned alongside the ToolMessages.
        """
        last_message: AIMessage = state["messages"][-1]

        async def _dispatch(tool_call: dict) -> ToolMessage:
            name = tool_call["name"]
            args = tool_call["args"]
            handler = _tool_dispatch.get(name)
            if handler is None:
                logger.warning("Unknown tool requested by agent: %s", name)
                content = json.dumps({"error": f"Unknown tool: {name}"})
            else:
                try:
                    content = await handler(args)
                except Exception:
                    logger.exception("Tool handler failed for %s", name)
                    content = json.dumps({"error": "Tool execution failed"})
            return ToolMessage(content=content, tool_call_id=tool_call["id"])

        tool_messages = await asyncio.gather(*[_dispatch(tc) for tc in last_message.tool_calls])

        # Merge any state patches accumulated by log_food_entries
        merged_patch: dict = {}
        for patch in _tool_state_patches:
            merged_patch.update(patch)
        _tool_state_patches.clear()

        return {"messages": list(tool_messages), **merged_patch}

    def should_continue(state: NutritionAgentState) -> str:
        """Route to tools if the last message has tool calls, otherwise end."""
        last_message = state["messages"][-1]
        if isinstance(last_message, AIMessage) and last_message.tool_calls:
            return "tools"
        return END

    # -----------------------------------------------------------------
    # Graph assembly
    # -----------------------------------------------------------------

    graph = StateGraph(NutritionAgentState)
    graph.add_node("agent", agent_node)
    graph.add_node("tools", tools_node)
    graph.add_edge(START, "agent")
    graph.add_conditional_edges("agent", should_continue, {"tools": "tools", END: END})
    graph.add_edge("tools", "agent")

    return graph.compile()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def run_nutrition_agent(
    text: str,
    user: User,
    meal_type: str,
    db: Session,
) -> str:
    """Run the LangGraph nutrition agent and return the formatted WhatsApp reply.

    Builds the graph, invokes it with an initial state, and extracts the final
    AI text response. FatSecret + local DB persistence is handled inside the
    graph's log_food_entries tool handler.
    """
    has_fatsecret = bool(user.fatsecret_access_token and user.fatsecret_access_secret)
    graph = _build_graph(user, meal_type, db)

    user_content = (
        f"User message: {text}\n\n"
        f"Context:\n"
        f"- meal_type: {meal_type}\n"
        f"- fatsecret_connected: {str(has_fatsecret).lower()}\n"
        f"- current_time: {datetime.now().strftime('%H:%M')}"
    )

    initial_state: NutritionAgentState = {
        "messages": [
            SystemMessage(content=AGENT_SYSTEM_PROMPT),
            HumanMessage(content=user_content),
        ],
        "meal_type": meal_type,
        "has_fatsecret": has_fatsecret,
        "food_description": "",
        "logged_items": [],
        "fatsecret_entry_ids": [],
    }

    final_state = await graph.ainvoke(initial_state)

    # Extract the last plain-text AI message as the reply
    for message in reversed(final_state["messages"]):
        if (
            isinstance(message, AIMessage)
            and isinstance(message.content, str)
            and message.content.strip()
            and not message.tool_calls
        ):
            return message.content

    logger.warning("Agent produced no final text response for user_id=%d", user.id)
    return "Meal logged!"
