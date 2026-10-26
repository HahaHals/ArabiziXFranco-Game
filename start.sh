#!/usr/bin/env sh
# Production start: bring the database schema up to date, then serve.
# --proxy-headers makes the app see players' real IPs behind the host's proxy
# (rate limiting depends on it).
set -e
alembic upgrade head
exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}" --proxy-headers --forwarded-allow-ips='*'
