#!/usr/bin/env sh
# Production start. The app applies database migrations itself on startup.
# --proxy-headers makes the app see players' real IPs behind the host's proxy.
set -e
exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}" --proxy-headers --forwarded-allow-ips='*'
