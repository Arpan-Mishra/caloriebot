import logging
import logging.config
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, Query, HTTPException, Depends
from fastapi.responses import PlainTextResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.config import get_settings

logging.config.dictConfig({
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "default": {"format": "%(asctime)s %(levelname)s %(name)s: %(message)s"},
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "default"},
        "file": {
            "class": "logging.FileHandler",
            "filename": "/tmp/calorie_bot.log",
            "formatter": "default",
        },
    },
    "root": {"level": "DEBUG", "handlers": ["console", "file"]},
})
from app.database import init_db, get_db, SessionLocal
from app.handlers.webhook import route_webhook
from app.services.scheduler import start_scheduler, shutdown_scheduler
from app.services.whatsapp import send_text_message

logger = logging.getLogger(__name__)
settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    init_db()
    db = SessionLocal()
    try:
        start_scheduler(db)
        from app.models import SystemConfig
        from app.services.whatsapp import set_whatsapp_token
        cfg = db.query(SystemConfig).filter(SystemConfig.key == "whatsapp_access_token").first()
        if cfg:
            set_whatsapp_token(cfg.value)
            logger.info("Loaded WhatsApp token override from DB")
    finally:
        db.close()
    yield
    # Shutdown
    shutdown_scheduler()


app = FastAPI(title="Calorie Counter WhatsApp Bot", lifespan=lifespan)



@app.get("/webhook")
async def verify_webhook(
    hub_mode: str = Query(None, alias="hub.mode"),
    hub_verify_token: str = Query(None, alias="hub.verify_token"),
    hub_challenge: str = Query(None, alias="hub.challenge"),
):
    """Meta webhook verification handshake."""
    if hub_mode == "subscribe" and hub_verify_token == settings.whatsapp_verify_token:
        return PlainTextResponse(hub_challenge)
    raise HTTPException(status_code=403, detail="Verification failed")


@app.post("/webhook")
async def receive_webhook(request: Request):
    """Receive and process incoming WhatsApp messages."""
    body = await request.json()

    async def _handle():
        db = SessionLocal()
        try:
            await route_webhook(body, db)
        finally:
            db.close()

    import asyncio
    asyncio.create_task(_handle())
    return {"status": "ok"}


@app.get("/health")
async def health():
    return {"status": "healthy"}


@app.get("/connect/fatsecret")
async def connect_fatsecret_start(request: Request, phone_number: str, db: Session = Depends(get_db)):
    """
    Step 1: Start FatSecret OAuth. Redirects browser to FatSecret authorization page.
    """
    from app.services.fatsecret import get_request_token
    from app.models import User, OAuthTemp
    from app.handlers.text_handler import _normalize_phone
    phone_number = _normalize_phone(phone_number)

    user = db.query(User).filter(User.phone_number == phone_number).first()
    if not user:
        user = User(phone_number=phone_number)
        db.add(user)
        db.commit()

    callback_url = str(request.base_url) + f"connect/fatsecret/callback?phone_number={phone_number}"
    oauth_token, oauth_token_secret, auth_url = get_request_token(callback_url=callback_url)

    # Persist token secret in DB so it survives restarts
    db.query(OAuthTemp).filter(OAuthTemp.phone_number == phone_number).delete()
    db.add(OAuthTemp(oauth_token=oauth_token, oauth_token_secret=oauth_token_secret, phone_number=phone_number))
    db.commit()

    return RedirectResponse(auth_url)


@app.get("/connect/fatsecret/callback")
async def connect_fatsecret_callback(
    oauth_token: str,
    oauth_verifier: str,
    phone_number: str,
    db: Session = Depends(get_db),
):
    """
    Step 2: Exchange verifier for access token and store on user.
    """
    from app.models import User, OAuthTemp
    from app.services.fatsecret import get_access_token
    from app.handlers.text_handler import _normalize_phone
    phone_number = _normalize_phone(phone_number)

    user = db.query(User).filter(User.phone_number == phone_number).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    temp = db.query(OAuthTemp).filter(OAuthTemp.oauth_token == oauth_token).first()
    if not temp:
        raise HTTPException(status_code=400, detail="OAuth session expired or not found. Please start the flow again.")

    oauth_token_secret = temp.oauth_token_secret
    db.delete(temp)
    db.commit()

    try:
        access_token, access_secret = get_access_token(oauth_token, oauth_token_secret, oauth_verifier)
    except Exception:
        logger.exception("FatSecret token exchange failed for phone_number=%s", phone_number)
        raise HTTPException(status_code=502, detail="FatSecret OAuth exchange failed. Please try connecting again.")

    user.fatsecret_access_token = access_token
    user.fatsecret_access_secret = access_secret
    db.commit()
    logger.info("FatSecret tokens stored for phone_number=%s", phone_number)

    try:
        await send_text_message(
            phone_number,
            "✅ FatSecret connected! Your account is now linked.\n\nGoing forward I'll log all your meals directly to your FatSecret diary with accurate nutrition data. Just tell me what you ate!",
        )
    except Exception:
        logger.exception("Failed to send FatSecret connected WhatsApp message to %s", phone_number)

    return {"status": "connected", "message": "FatSecret account linked successfully! You can close this tab and return to WhatsApp."}


@app.get("/admin/token-status")
async def token_status(secret: str, db: Session = Depends(get_db)):
    """Diagnostic endpoint: show which WhatsApp token is active at runtime."""
    if not settings.admin_secret or secret != settings.admin_secret:
        raise HTTPException(status_code=403, detail="Invalid secret")

    from app.models import SystemConfig
    from app.services.whatsapp import _token_override, _get_token

    active = _get_token()
    cfg = db.query(SystemConfig).filter(SystemConfig.key == "whatsapp_access_token").first()

    return {
        "override_set": _token_override is not None,
        "db_token_stored": cfg is not None,
        "active_token_suffix": active[-8:] if active else None,
        "active_token_length": len(active) if active else None,
        "db_token_suffix": cfg.value[-8:] if cfg else None,
        "db_token_length": len(cfg.value) if cfg else None,
    }


@app.post("/admin/update-whatsapp-token")
async def update_whatsapp_token(request: Request, db: Session = Depends(get_db)):
    """Update the WhatsApp access token at runtime without redeploying."""
    from app.models import SystemConfig
    from app.services.whatsapp import set_whatsapp_token
    import json as _json

    raw = await request.body()
    # Strip control characters (newlines, carriage returns, tabs) before JSON parsing
    # so tokens pasted with terminal line-wrapping are handled correctly
    sanitized = raw.replace(b'\n', b'').replace(b'\r', b'').replace(b'\t', b'')
    body = _json.loads(sanitized)
    if not settings.admin_secret or body.get("secret") != settings.admin_secret:
        raise HTTPException(status_code=403, detail="Invalid secret")

    token = "".join(body.get("token", "").split())  # strip all whitespace including newlines
    if not token:
        raise HTTPException(status_code=400, detail="token is required")

    cfg = db.query(SystemConfig).filter(SystemConfig.key == "whatsapp_access_token").first()
    if cfg:
        cfg.value = token
    else:
        db.add(SystemConfig(key="whatsapp_access_token", value=token))
    db.commit()

    set_whatsapp_token(token)
    logger.info("WhatsApp access token updated via admin endpoint")
    return {"status": "updated"}
