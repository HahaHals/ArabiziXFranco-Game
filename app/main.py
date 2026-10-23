import logging
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from app import owner, play
from app.auth import NotLoggedIn
from app.config import DEV_SECRET_KEY, get_config
from app.db import get_db

config = get_config()
if config.secret_key == DEV_SECRET_KEY:
    logging.getLogger(__name__).warning("SECRET_KEY is not set; using an insecure dev key")

app = FastAPI(title="Franco Dataset Game")
app.add_middleware(
    SessionMiddleware,
    secret_key=config.secret_key,
    session_cookie="franco_session",
    max_age=config.session_days * 24 * 3600,
    same_site="lax",  # also blocks cross-site form posts to the owner panel
    https_only=config.cookie_secure,
)
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
app.include_router(owner.router)
app.include_router(play.router)


@app.exception_handler(NotLoggedIn)
def _to_login(request: Request, exc: NotLoggedIn):
    return RedirectResponse("/owner/login", status_code=303)


@app.get("/health")
def health(db: Session = Depends(get_db)) -> dict[str, str]:
    db.execute(text("SELECT 1"))
    return {"status": "ok"}
