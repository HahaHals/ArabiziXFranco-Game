# Franco Dataset Game

A small web game that collects how people naturally write Egyptian Arabic words in
Franco (Arabizi). See `franco-game-spec.md` for the full spec.

## Local setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
cp .env.example .env            # defaults to SQLite at ./franco.db
.venv/bin/alembic upgrade head  # create/upgrade the schema
.venv/bin/uvicorn app.main:app --reload
```

Create the first owner account in the browser: set `SETUP_CODE` in `.env` (12+
characters), open http://localhost:8000/owner, and fill in the form. That page
switches itself off once an owner exists. Owners change their password from
"Your account" (click your username in the panel).

Or use the command line:

```bash
.venv/bin/python -m app.cli create-owner <username>   # prompts for a password
.venv/bin/python -m app.cli set-password <username>
.venv/bin/python -m app.cli list-owners
```

In production, set a random `SECRET_KEY` and `COOKIE_SECURE=true`.

Sentence generation needs `ANTHROPIC_API_KEY` (and optionally `ANTHROPIC_MODEL`). Without a key the game
still runs on owner-added sentences; the Generation page says the key is missing.

Run tests with `.venv/bin/pytest`.

## Layout

- `app/config.py`: deployment config from env vars (`DATABASE_URL`, `SECRET_KEY`, `ANTHROPIC_*`)
- `app/db.py`: engine, session, declarative base
- `app/models.py`: all tables (spec section 8)
- `app/settings_store.py`: owner-tunable settings stored in the `settings` table, with defaults
- `app/auth.py`: owner passwords (argon2), login sessions, `require_owner`
- `app/owner.py`: owner panel routes; `app/templates/`, `app/static/`: pages and CSS
- `app/cli.py`: owner account commands
- `app/generation.py`: batch generation with Claude (prompt, API call, auto-trigger)
- `migrations/`: Alembic migrations

## Changing the schema

Edit `app/models.py`, then:

```bash
.venv/bin/alembic revision --autogenerate -m "describe the change"
.venv/bin/alembic upgrade head
```

Review the generated file before committing. `alembic check` fails if models and
migrations have drifted.
