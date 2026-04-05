# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Coding Standards

See `.claude/rules/coding-best-practices.md` for required conventions covering logging, Pydantic validation, exception handling, PEP 8, and general Python best practices. These rules apply to all files under `app/`.

---

## Running the server

```bash
# Start in background (logs to /tmp/calorie_bot.log)
nohup venv/bin/uvicorn app.main:app --port 8000 >> /tmp/calorie_bot.log 2>&1 &

# Start in foreground with hot-reload (development)
venv/bin/uvicorn app.main:app --reload --port 8000

# Verify it's up
curl http://localhost:8000/health

# Watch logs
tail -f /tmp/calorie_bot.log

# Stop the background server
pkill -f "uvicorn app.main:app"
```

---

## Environment

All config is loaded from `.env` via `app/config.py` (pydantic-settings). Required variables:

```
WHATSAPP_PHONE_NUMBER_ID=
WHATSAPP_ACCESS_TOKEN=        # expires every 24h on Meta sandbox — regenerate in Meta Developer Console
WHATSAPP_VERIFY_TOKEN=        # arbitrary secret string, must match what's set in Meta webhook config

OPENAI_API_KEY=               # used for Whisper transcription (fallback if Groq unavailable)
ANTHROPIC_API_KEY=            # used for all Claude calls
GROQ_API_KEY=                 # optional — Groq Whisper for fast transcription (~10x faster than OpenAI)

FATSECRET_CONSUMER_KEY=       # optional — only needed for FatSecret integration
FATSECRET_CONSUMER_SECRET=    # optional

DATABASE_URL=sqlite:///./calorie_bot.db

ADMIN_SECRET=                 # arbitrary secret string used to authenticate admin endpoints
```

Config is cached via `@lru_cache` — restart the server after changing `.env`.

---

## Architecture Overview

```
app/
├── main.py                  # FastAPI app, lifespan, HTTP endpoints
├── config.py                # pydantic-settings config (reads .env)
├── database.py              # SQLAlchemy engine, session factory, Base, init_db
├── models.py                # ORM table definitions
├── schemas.py               # Pydantic request/response schemas
├── handlers/
│   ├── webhook.py           # Routes incoming webhook events to text or voice handler
│   ├── text_handler.py      # Intent detection, ack, delegates food logging to nutrition agent
│   └── voice_handler.py     # Sends ack, downloads audio, transcribes, delegates to text_handler
└── services/
    ├── whatsapp.py          # Meta Cloud API: send messages, download media, parse payloads
    ├── nutrition.py         # Claude API: intent detection (Haiku) + reminder parsing (Sonnet)
    ├── nutrition_agent.py   # LangGraph agentic pipeline: 6-phase food search + logging loop
    ├── fatsecret.py         # FatSecret OAuth, food search, diary logging, daily totals
    ├── transcription.py     # OpenAI Whisper transcription
    └── scheduler.py         # APScheduler: reminder job management
```

**Dependencies** (beyond standard FastAPI/SQLAlchemy stack):
- `langchain-anthropic` + `langgraph` — nutrition agent
- `anthropic` — intent detection and reminder parsing (nutrition.py)
- `fatsecret` (pyfatsecret) — FatSecret Platform API via OAuth 1.0a

---

## Request Flow

```
WhatsApp message (POST /webhook)
  → main.py: asyncio.create_task(_handle())          # fire-and-forget, returns 200 immediately
  → handlers/webhook.py: route_webhook()              # parse payload, identify message type
      → handle_text()   (text messages)
      → handle_voice()  (audio/voice notes)
            ↓ voice path
          send_text_message("🎙️ Voice note received!...")  # immediate ack before slow work
          whatsapp.download_media()                    # fetch raw audio bytes via Graph API
          transcription.transcribe_audio()             # Whisper → plain text
            ↓ both paths converge
  → text_handler.handle_text()
      send_text_message("🍽️ Got it! I'm logging...")    # ack for text (skipped for voice)
      asyncio.gather(
          asyncio.to_thread(is_reminder_request()),   # Haiku — runs concurrently
          asyncio.to_thread(is_summary_request()),    # Haiku — runs concurrently
      )
      → if reminder: _handle_reminder()
      → if summary:  _daily_summary()
      → else: nutrition_agent.run_nutrition_agent()
            ↓ LangGraph loop (Phases 1–6)
          Phase 1: parse items + quantities (no tools)
          Phase 2: search_food IN PARALLEL per item
          Phase 3: select best match, compute number_of_units, scale macros
          Phase 4: log_food_entries → FatSecret diary + local SQLite
          Phase 5: get_today_totals → FatSecret diary (source of truth)
          Phase 6: format WhatsApp reply
      → whatsapp.send_text_message()                  # final reply
```

---

## Agentic Nutrition Pipeline

`app/services/nutrition_agent.py` implements the food-logging agent using **LangGraph** + **Claude Sonnet 4.6** (`langchain-anthropic`).

### Graph topology

```
START ──► agent ──► tools ──► agent ──► … ──► END
               └─────────────────────────────────►
```

- **`agent` node** — calls `ChatAnthropic` with 4 tools bound. Routes to `tools` if tool calls present, else `END`.
- **`tools` node** — custom async node; all tool calls within one agent turn run concurrently via `asyncio.gather`. Merges state patches from `log_food_entries` into graph state.
- A fresh graph is compiled per request via `_build_graph(user, meal_type, db)`.

### Agent state (`NutritionAgentState`)

All fields are designed as future memory anchors — adding a LangGraph checkpointer + `thread_id` will give persistent per-user memory with no state schema changes.

| Field | Type | Set by | Purpose |
|-------|------|--------|---------|
| `messages` | `list` (add_messages) | LangGraph / agent node | Full conversation history. Primary memory surface. |
| `meal_type` | `str` | `run_nutrition_agent` at invocation | Meal type from time of day; stored for temporal recall. |
| `has_fatsecret` | `bool` | `run_nutrition_agent` at invocation | Snapshot of token presence at log time. |
| `food_description` | `str` | `log_food_entries` tool | Human-readable meal description. **Memory anchor.** |
| `logged_items` | `list[dict]` | `log_food_entries` tool | Per-item macros. **Memory anchor.** |
| `fatsecret_entry_ids` | `list[str]` | `log_food_entries` tool | FatSecret diary IDs. **Memory anchor.** |

#### Adding memory (future)

```python
from langgraph.checkpoint.sqlite import SqliteSaver
saver = SqliteSaver.from_conn_string("./agent_memory.db")
graph = graph_builder.compile(checkpointer=saver)
config = {"configurable": {"thread_id": str(user.id)}}
final_state = await graph.ainvoke(initial_state, config=config)
```

### Tools

| Tool | Description | API calls |
|------|-------------|-----------|
| `search_food` | Autocomplete → search → score → fetch details for top 5 | ~7 calls total |
| `log_food_entries` | Log to FatSecret diary + local SQLite | 1 call per item |
| `get_today_totals` | Full-day totals from FatSecret diary (FS users) or local DB | 1 call |
| `get_meal_type` | Returns meal_type from state | 0 calls |

### 6-phase system prompt

The agent works through explicit phases before making any tool calls:

1. **Parse** — extract `item_name`, `user_quantity`, `search_query` for every food item
2. **Search** — `search_food` for each item in parallel (no quantities in query)
3. **Select + calculate** — pick best match by `match_score`; compute `number_of_units` and scale macros
4. **Log** — `log_food_entries` once with all items
5. **Totals** — `get_today_totals` (result already includes this meal)
6. **Reply** — format WhatsApp message

---

## FatSecret Integration Details

### Food search strategy

`search_food` runs 3 steps to minimise API calls and maximise match quality:

1. `foods_autocomplete(query, max_results=30)` — maps colloquial name to FatSecret's canonical name (e.g. "pintola protein oats" → "High Protein Oats (Pintola)"). Falls back to original query on failure.
2. `foods_search(canonical_name, max_results=30)` — returns food names + IDs cheaply (no macros yet). Falls back to original query if canonical yields no results.
3. Score all results by `_similarity_score(original_query, food_name)` — zero API calls. Take top 5 by score, call `food_get` for those 5 only.

**Rate limiting**: fetching `food_get` for all 30 results triggered FatSecret's rate limiter and caused `food_entry_create` to fail. The 2-phase approach (score first, fetch only top 5) keeps total calls to ~7 per item.

### Similarity scoring (`_similarity_score`)

Word-overlap score with a bonus for brand names in parentheses:
- Tokenises both strings, removes noise words (g, ml, oz, serving, etc.)
- Base score = `|overlap| / |query_tokens|`
- Brand bonus: +0.2 per query word that matches a brand name in `(parentheses)` in the result

### FatSecret `number_of_units` unit bug — critical

**`food_entry_create(number_of_units=X)`** means **X of the serving's `measurement_description`**, not X servings.

For chicken breast: `measurement_description="g"`, `serving_number_of_units=100`.
- Passing `number_of_units=2` → logs **2g** ❌
- Passing `number_of_units=200` → logs **200g** ✓

**Formula used in `log_food_entries_batch`:**
```
fatsecret_units = agent_servings × serving_number_of_units
```
Where:
- `agent_servings` = what the agent calculated (e.g. `user_g / metric_serving_amount`)
- `serving_number_of_units` = FatSecret's `number_of_units` field from the serving dict (carried through from `search_food` results unchanged)

Examples:
- 200g chicken (serving `number_of_units=100`): `2.0 × 100 = 200` → logs 200g ✓
- 2 eggs (serving `number_of_units=1`): `2.0 × 1 = 2` → logs 2 servings ✓
- 84g oats, 42g serving (serving `number_of_units=1`): `2.0 × 1 = 2` → logs 2 servings ✓

### Daily totals (`get_food_entries_today`)

When FatSecret is connected, `get_today_totals` calls `food_entries_get(date=datetime.today())` — the FatSecret diary is the source of truth, not the local SQLite DB. The local DB only stores estimates for users without FatSecret.

`pyfatsecret.valid_response` unwraps the JSON and returns a **plain `list[dict]`** for `food_entries`. Do not call `.get("food_entries", {})` on the response — it's already the list.

### Meal type mapping

FatSecret only accepts: `breakfast`, `lunch`, `dinner`, `other`. `"snack"` maps to `"other"`.

### OAuth flow

To link a user's FatSecret account: visit `GET /connect/fatsecret?phone_number=<number>` in a browser and complete the OAuth flow.

---

## Module Reference

### `app/main.py`
FastAPI application entry point.

- **`lifespan`** — startup: `init_db()`, starts scheduler; shutdown: stops scheduler
- **`GET /webhook`** — Meta webhook verification handshake
- **`POST /webhook`** — receives WhatsApp events; fires `route_webhook` as background task
- **`GET /health`** — health check
- **`GET /connect/fatsecret`** — starts FatSecret OAuth; stores `oauth_token` + `oauth_token_secret` in `OAuthTemp` DB table (survives restarts)
- **`GET /connect/fatsecret/callback`** — exchanges verifier for access token; deletes `OAuthTemp` row; stores tokens on `User`
- **`GET /admin/token-status`** — diagnostic: returns active token suffix/length and whether DB override is set; requires `secret` query param
- **`GET /admin/clear-token-override`** — removes DB token override so env var takes effect; requires `secret` query param
- **`POST /admin/update-whatsapp-token`** — updates token in DB + in-memory at runtime; requires `secret` in JSON body

---

### `app/handlers/text_handler.py`
Core business logic.

- **`handle_text(db, phone_number, text, ack_sent=False)`** — sends immediate ack (unless `ack_sent=True`), runs intent detection concurrently via `asyncio.gather`, then delegates food logging to `nutrition_agent.run_nutrition_agent()`.
- **`_infer_meal_type()`** — time-based meal type (never asks user):

| Time | Meal type |
|------|-----------|
| 05:00 – 10:59 | breakfast |
| 11:00 – 14:59 | lunch |
| 15:00 – 18:59 | snack |
| 19:00 – 04:59 | dinner |

- **`_daily_summary(db, user)`** — queries local `MealEntry` rows and formats summary.
- **`_handle_reminder(db, user, phone_number, text)`** — parses reminder via Claude, saves to `Reminder`, registers with APScheduler.
- **`_normalize_phone(phone_number)`** — strips `+` prefix; all numbers stored as bare digits.

---

### `app/handlers/voice_handler.py`
Handles `audio` type messages:

1. `send_text_message("🎙️ Voice note received!...")` — immediate ack **before** download
2. `download_media(media_id)` — fetch raw bytes
3. `transcribe_audio(bytes, mime_type)` — Whisper → text
4. `handle_text(..., ack_sent=True)` — delegate (skip second ack)

---

### `app/services/nutrition.py`
Claude API calls for intent detection and reminder parsing. Uses synchronous `anthropic.Anthropic`.

| Function | Model | Purpose |
|----------|-------|---------|
| `parse_reminder(text)` | `claude-sonnet-4-6` | Parses reminder → `ReminderConfig` with cron expression |
| `is_reminder_request(text)` | `claude-haiku-4-5-20251001` | "yes"/"no" intent detection |
| `is_summary_request(text)` | `claude-haiku-4-5-20251001` | "yes"/"no" intent detection |

`parse_nutrition` still exists but is no longer called in the main flow.

---

### `app/services/nutrition_agent.py`
LangGraph agent. See *Agentic Nutrition Pipeline* section above.

Key symbols:
- **`NutritionAgentState`** — `TypedDict` graph state (see table above)
- **`TOOL_SCHEMAS`** — Anthropic-format tool dicts for `ChatAnthropic.bind_tools`
- **`AGENT_SYSTEM_PROMPT`** — 6-phase instruction prompt
- **`_build_graph(user, meal_type, db)`** — creates closures + compiles graph; called once per request
- **`run_nutrition_agent(text, user, meal_type, db)`** — public entry point; returns WhatsApp reply string

---

### `app/services/fatsecret.py`
FatSecret Platform API via `pyfatsecret` (OAuth 1.0a).

- **`_similarity_score(query, food_name)`** — word-overlap score with brand-name bonus; used to rank search results without extra API calls
- **`_autocomplete_query(client, query)`** — calls `foods_autocomplete(max_results=30)`, picks suggestion most similar to query; falls back to original on error
- **`search_food(query, access_token, access_secret)`** — full 3-step search (autocomplete → search 30 → score → food_get top 5); returns list of result dicts including `serving_number_of_units`
- **`log_food_entries_batch(items, meal_type, access_token, access_secret)`** — logs items to FatSecret diary; computes `fatsecret_units = agent_servings × serving_number_of_units` to handle FatSecret's unit system correctly
- **`get_food_entries_today(access_token, access_secret)`** — fetches today's diary via `food_entries_get(date=today)`; returns summed `{calories, protein_g, fat_g, carbs_g, meal_count}`
- **`get_request_token(callback_url)`** — OAuth step 1
- **`get_access_token(oauth_token, oauth_token_secret, oauth_verifier)`** — OAuth step 2
- **`log_food_entry(...)`** — legacy single-item search-and-log (kept for compatibility, not used in main flow)

---

### `app/services/transcription.py`
OpenAI Whisper wrapper.

- **`transcribe_audio(audio_bytes, mime_type)`** — wraps bytes in `BytesIO`, calls `client.audio.transcriptions.create(model="whisper-1")`, returns text string

---

### `app/services/scheduler.py`
APScheduler (`AsyncIOScheduler`) for meal reminders.

- **`add_reminder_job(reminder_id, phone_number, cron_expression, message)`** — registers/replaces cron job; job ID is `reminder_{id}`
- **`remove_reminder_job(reminder_id)`** — removes job if it exists
- **`load_reminders_from_db(db)`** — called at startup; queries `active=True` reminders and registers them
- **`start_scheduler(db)`** + **`shutdown_scheduler()`** — lifecycle

Cron expressions: standard 5-field `minute hour day month day_of_week`.

---

## Models (`app/models.py`)

| Model | Table | Key columns |
|-------|-------|-------------|
| `User` | `users` | `phone_number` (unique), `fatsecret_access_token`, `fatsecret_access_secret` |
| `MealEntry` | `meal_entries` | `user_id`, `meal_type`, `food_description`, `calories`, `protein_g`, `fat_g`, `carbs_g`, `logged_at`, `fatsecret_entry_id` |
| `ConversationState` | `conversation_states` | `user_id` (unique), `state`, `pending_data` — exists in DB but no longer used for multi-turn flows |
| `Reminder` | `reminders` | `user_id`, `label`, `cron_expression`, `message`, `active` |
| `SystemConfig` | `system_config` | `key` (primary key), `value` — runtime-configurable app settings |
| `OAuthTemp` | `oauth_temp` | `oauth_token` (unique), `oauth_token_secret`, `phone_number` — transient FatSecret OAuth state; rows deleted after callback |

`MealEntry.fatsecret_entry_id` stores comma-separated FatSecret entry IDs.

`OAuthTemp` replaced the original in-memory `_oauth_secrets` dict so OAuth flows survive server restarts.

---

## Phone Number Format

All numbers stored as bare digits, no `+` prefix (e.g. `919958325792`). `_normalize_phone()` in `text_handler.py` enforces this on every lookup/insert. FatSecret OAuth callback also normalises.

---

## Logging

All logs go to `/tmp/calorie_bot.log` and stdout at `DEBUG` level. Key log points:

| Log message | Source | Meaning |
|-------------|--------|---------|
| `[phone] Incoming text: ...` | text_handler | Every message received |
| `[phone] Inferred meal_type=...` | text_handler | Time-based meal type |
| `Autocomplete %r → %r (score=...)` | fatsecret | Canonical name resolved |
| `search_food %r: top 5 of N — [...]` | fatsecret | Candidates selected for food_get |
| `search_food %r → N results, best=...` | fatsecret | Final scored results |
| `FatSecret entry created: id=... agent_servings=... fatsecret_units=...` | fatsecret | Diary entry confirmed with unit details |
| `FatSecret today (%s): %d entries — cal=...` | fatsecret | Daily totals from FatSecret API |
| `Agent iteration %d: stop_reason=... blocks=...` | nutrition_agent | Each LangGraph loop iteration |
| `Logged meal entry id=...: ...` | nutrition_agent | DB entry confirmed |
