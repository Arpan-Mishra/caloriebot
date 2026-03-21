# CalorieBot

A WhatsApp bot that logs your meals and tracks daily nutrition using voice or text. Powered by Claude (Anthropic) for food parsing, FatSecret for a searchable food database and diary, and OpenAI Whisper for voice transcription.

---

## Features

- **Log meals by text or voice** — describe what you ate naturally ("had 2 eggs and a bowl of oats") and the bot parses, searches, and logs everything
- **FatSecret diary sync** — entries are written directly to your FatSecret account with accurate macros
- **Daily summary** — ask for today's totals at any time
- **Meal reminders** — set recurring reminders ("remind me to log lunch at 1pm every weekday")
- **Smart food search** — autocomplete + similarity scoring finds the right item even with colloquial names
- **Agentic pipeline** — LangGraph-powered multi-phase agent handles multi-item meals in a single message

---

## System Requirements

- Python 3.12+
- A [Meta Developer](https://developers.facebook.com/) app with WhatsApp Cloud API enabled
- An [Anthropic API](https://console.anthropic.com/) key
- An [OpenAI API](https://platform.openai.com/) key (for Whisper transcription)
- A [FatSecret Platform](https://platform.fatsecret.com/) app (optional — enables diary sync)

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
OPENAI_API_KEY=                  # whisper-1 for voice transcription

# FatSecret (optional)
FATSECRET_CONSUMER_KEY=
FATSECRET_CONSUMER_SECRET=

# Database
DATABASE_URL=sqlite:///./calorie_bot.db

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

## FatSecret OAuth Setup

To link a user's FatSecret account, open this URL in a browser:

```
https://<your-domain>/connect/fatsecret?phone_number=<phone_number>
```

Complete the OAuth flow and the user's tokens are stored automatically. The bot will start syncing meals to their FatSecret diary immediately.

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
    ├── fatsecret.py         # FatSecret search, diary logging, OAuth
    ├── transcription.py     # OpenAI Whisper wrapper
    └── scheduler.py         # APScheduler reminder jobs
```

### Request flow

```
WhatsApp message → POST /webhook
  → fire-and-forget task (200 returned immediately)
  → route by message type (text / voice)
      voice: ack → download → Whisper → text
  → handle_text()
      → concurrent intent detection (reminder? summary?)
      → if food: LangGraph agent
          Phase 1: parse items + quantities
          Phase 2: search_food per item (parallel)
          Phase 3: select best match, scale macros
          Phase 4: log_food_entries → FatSecret + SQLite
          Phase 5: get_today_totals
          Phase 6: format reply
      → send WhatsApp reply
```

---

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/webhook` | Meta webhook verification handshake |
| `POST` | `/webhook` | Receive WhatsApp messages |
| `GET` | `/health` | Health check |
| `GET` | `/connect/fatsecret` | Start FatSecret OAuth flow |
| `GET` | `/connect/fatsecret/callback` | FatSecret OAuth callback |
| `GET` | `/admin/token-status` | Show active token info (requires `secret`) |
| `GET` | `/admin/clear-token-override` | Clear DB token override (requires `secret`) |
| `POST` | `/admin/update-whatsapp-token` | Update token at runtime (requires `secret` in body) |

---

## License

MIT
