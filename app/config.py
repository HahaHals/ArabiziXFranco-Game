"""Deployment config, read from environment variables (or a local .env file).

Game tuning knobs that owners change at runtime live in the `settings` table
instead; see app/settings_store.py.
"""

from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


DEV_SECRET_KEY = "dev-insecure-secret"
SETUP_CODE_MIN_LENGTH = 12


class Config(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "sqlite:///./franco.db"
    secret_key: str = DEV_SECRET_KEY
    # Send the owner session cookie over HTTPS only. Turn on in production.
    cookie_secure: bool = False
    session_days: int = 7
    # Unlocks the first-time "create owner account" page while no owner exists.
    # Empty (or shorter than SETUP_CODE_MIN_LENGTH) keeps that page switched off.
    setup_code: str = ""
    anthropic_api_key: str = ""
    # The cheapest current model. Switch to claude-sonnet-5-5 if sentences come out unnatural.
    anthropic_model: str = "claude-haiku-5-5"

    @field_validator("database_url")
    @classmethod
    def _use_psycopg(cls, url: str) -> str:
        # Hosts like Neon hand out "postgresql://..." (or "postgres://..."); SQLAlchemy
        # needs to be told to use the psycopg 3 driver we install.
        for prefix in ("postgres://", "postgresql://"):
            if url.startswith(prefix):
                return "postgresql+psycopg://" + url[len(prefix):]
        return url


@lru_cache
def get_config() -> Config:
    return Config()
