import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app import owner, play
from app.auth import NotLoggedIn
from app.config import DEV_SECRET_KEY, get_config
from app.db import get_db

config = get_config()
if config.secret_key == DEV_SECRET_KEY:
    logging.getLogger(__name__).warning("SECRET_KEY is not set; using an insecure dev key")

def migrate() -> None:
    """Bring the database schema up to date. Runs on every start, so any host that just
    starts the app (without our start script) still gets the migrations."""
    from alembic import command
    from alembic.config import Config as AlembicConfig

    cfg = AlembicConfig()  # no ini file, so Alembic leaves the server's logging alone
    cfg.set_main_option("script_location", str(Path(__file__).parent.parent / "migrations"))
    command.upgrade(cfg, "head")


@asynccontextmanager
async def lifespan(app: FastAPI):
    if config.migrate_on_start:
        migrate()
    yield


app = FastAPI(title="Franco Dataset Game", lifespan=lifespan)
app.add_middleware(
    SessionMiddleware,
    secret_key=config.secret_key,
    session_cookie="franco_session",
    max_age=config.session_days * 24 * 3600,
    same_site="lax",  # also blocks cross-site form posts to the owner panel
    https_only=config.cookie_secure,
)
# Hosts put a proxy in front of the app; read the player's real IP from it (rate limiting
# depends on it). Added last, so it runs first.
app.add_middleware(ProxyHeadersMiddleware, trusted_hosts="*")
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
