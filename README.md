# NutriBot

A WhatsApp bot that logs your meals and tracks daily nutrition using voice or text. Powered by Claude (Anthropic) for food parsing, NutriChat for a searchable food database and diary, and Groq Whisper for fast voice transcription (OpenAI Whisper fallback).

---

## Features

- **Log meals by text or voice** — describe what you ate naturally ("had 2 eggs and a bowl of oats") and the bot parses, searches, and logs everything
- **NutriChat diary sync** — entries are written directly to your NutriChat account with accurate macros
- **Daily summary** — ask for today's totals at any time
- **Meal reminders** — set recurring reminders ("remind me to log lunch at 1pm every weekday")
- **Smart food search** — similarity scoring finds the right item even with colloquial names
- **Delete entries** — remove logged entries by meal type or for the whole day
- **Timezone-aware meal type** — infers breakfast/lunch/dinner/snack from the user's local time based on their phone number's country code
- **Agentic pipeline** — LangGraph-powered multi-phase agent handles multi-item meals in a single message
- **Short-term memory** — MongoDB-backed LangGraph checkpointer preserves conversation context across turns

---

## System Requirements

- Python 3.12+
- A [Meta Developer](https://developers.facebook.com/) app with WhatsApp Cloud API enabled
- An [Anthropic API](https://console.anthropic.com/) key
- A [Groq API](https://console.groq.com/) key (optional — faster transcription; falls back to OpenAI)
- An [OpenAI API](https://platform.openai.com/) key (Whisper fallback for voice transcription)
- A [MongoDB](https://www.mongodb.com/) instance (optional — enables short-term agent memory)

---

## Installation

```bash
git clone https://github.com/Arpan-Mishra/caloriebot.git
cd caloriebot

python3.12 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

---

## Configuration

Copy `.env.example` to `.env` and fill in the values:

```env
# WhatsApp / Meta Cloud API
WHATSAPP_PHONE_NUMBER_ID=        # from Meta Developer Console → WhatsApp → API Setup
WHATSAPP_ACCESS_TOKEN=           # from Meta Developer Console (regenerate when expired)
WHATSAPP_VERIFY_TOKEN=           # arbitrary string — must match Meta webhook config

# AI APIs
ANTHROPIC_API_KEY=               # claude-sonnet-4-6 + claude-haiku-4-5 for intent + parsing
GROQ_API_KEY=                    # optional — Groq Whisper (whisper-large-v3-turbo, ~10x faster)
OPENAI_API_KEY=                  # whisper-1 fallback for voice transcription

# NutriChat (optional — per-user API key linked via "link" command)
NUTRICHAT_BASE_URL=              # defaults to https://nutrichat-production.up.railway.app

# FatSecret (optional — legacy fallback)
FATSECRET_CONSUMER_KEY=
FATSECRET_CONSUMER_SECRET=

# Database
DATABASE_URL=sqlite:///./calorie_bot.db
MONGODB_URI=                     # optional — MongoDB connection for LangGraph checkpointer

# Admin
ADMIN_SECRET=                    # protects /admin/* endpoints
```

> Config is loaded via `pydantic-settings` and cached at startup. Restart the server after changing `.env`.

---

## Running Locally

```bash
# Development (hot-reload)
venv/bin/uvicorn app.main:app --reload --port 8000

# Background (production-like)
nohup venv/bin/uvicorn app.main:app --port 8000 >> /tmp/calorie_bot.log 2>&1 &

# Verify
curl http://localhost:8000/health

# Logs
tail -f /tmp/calorie_bot.log

# Stop background server
pkill -f "uvicorn app.main:app"
```

You'll need a public URL for Meta's webhook. Use [ngrok](https://ngrok.com/) or similar during development:

```bash
ngrok http 8000
```

Set the forwarding URL as your webhook in the Meta Developer Console, and configure `WHATSAPP_VERIFY_TOKEN` to match.

---

## Deployment

The project is configured for [Railway](https://railway.app/). Push to `main` to deploy automatically.

Required Railway environment variables mirror the `.env` values above. The `DATABASE_URL` can remain SQLite for single-instance deployments.

---

## NutriChat Account Linking

To connect a user's NutriChat account, they send:

```
link nutrichat_live_YOUR_API_KEY
```

The bot validates the key against the NutriChat API and stores it. Once linked, all meals are logged to the NutriChat diary and totals are pulled from there instead of local estimates.

Users can find their API key in the NutriChat app settings.

---

## Architecture

```
app/
├── main.py                  # FastAPI app, lifespan, HTTP endpoints
├── config.py                # pydantic-settings (reads .env)
├── database.py              # SQLAlchemy engine + session factory
├── models.py                # ORM models
├── schemas.py               # Pydantic schemas
├── handlers/
│   ├── webhook.py           # Routes incoming WhatsApp events
│   ├── text_handler.py      # Intent detection + food logging orchestration
│   └── voice_handler.py     # Voice note download, transcription, delegation
└── services/
    ├── whatsapp.py          # Meta Cloud API client
    ├── nutrition.py         # Claude intent detection + reminder parsing
    ├── nutrition_agent.py   # LangGraph 6-phase food logging agent
    ├── nutrichat_svc.py     # NutriChat search, diary logging, deletion
    ├── fatsecret.py         # FatSecret search + diary (legacy fallback)
    ├── transcription.py     # Groq Whisper (primary) + OpenAI Whisper (fallback)
    └── scheduler.py         # APScheduler reminder jobs
```

### Request flow

```
WhatsApp message → POST /webhook
  → fire-and-forget task (200 returned immediately)
  → route by message type (text / voice)
      voice: ack → download → Groq/OpenAI Whisper → text
  → handle_text()
      → concurrent intent detection (reminder? summary? delete?)
      → if food: LangGraph agent (MongoDB checkpointer for memory)
          Phase 1: parse items + quantities
          Phase 2: search_food per item (parallel)
          Phase 3: select best match, scale macros
          Phase 4: log_food_entries → NutriChat + SQLite
          Phase 5: get_today_totals
          Phase 6: format reply
      → send WhatsApp reply
```

### Voice transcription

`transcription.py` tries Groq first (`whisper-large-v3-turbo`, ~10× faster), falling back to OpenAI `whisper-1` if the Groq key is absent or the call fails.

### Agent memory

When `MONGODB_URI` is set, `main.py` initialises a `MongoDBSaver` checkpointer at startup and registers it with the nutrition agent via `set_checkpointer()`. The agent passes `thread_id=user.id` to LangGraph so each user's conversation history is persisted across turns.

### Meal type inference

`_infer_meal_type(phone_number)` resolves the user's timezone from their phone number's country code (`phonenumbers` library) and maps the local hour to breakfast / lunch / dinner / snack. If the user explicitly mentions a meal type in their message that takes priority. Falls back to UTC when the timezone cannot be resolved.

---

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/webhook` | Meta webhook verification handshake |
| `POST` | `/webhook` | Receive WhatsApp messages |
| `GET` | `/health` | Health check |
| `GET` | `/connect/fatsecret` | Start FatSecret OAuth flow (legacy) |
| `GET` | `/connect/fatsecret/callback` | FatSecret OAuth callback (legacy) |
| `GET` | `/admin/token-status` | Show active token info (requires `secret`) |
| `GET` | `/admin/clear-token-override` | Clear DB token override (requires `secret`) |
| `POST` | `/admin/update-whatsapp-token` | Update token at runtime (requires `secret` in body) |

---

## License

MIT
