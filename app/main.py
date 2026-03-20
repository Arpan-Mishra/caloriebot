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

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    init_db()
    db = SessionLocal()
    try:
        start_scheduler(db)
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

    access_token, access_secret = get_access_token(oauth_token, oauth_token_secret, oauth_verifier)
    user.fatsecret_access_token = access_token
    user.fatsecret_access_secret = access_secret
    db.commit()

    return {"status": "connected", "message": "FatSecret account linked successfully! You can close this tab and return to WhatsApp."}
