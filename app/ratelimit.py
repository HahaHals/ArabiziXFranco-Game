"""In-memory rate limiting per IP and per player (spec 7).

IPs are only held in memory for a minute; they're never written to the database.
Assumes a single server process, which is all this game needs.
"""

import threading
import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request

WINDOW = 60.0
# Friends on the same Wi-Fi share one IP, so an IP gets several players' worth.
IP_MULTIPLIER = 5

_hits: dict[str, deque] = defaultdict(deque)
_lock = threading.Lock()


def _allow(key: str, limit: int, now: float) -> bool:
    with _lock:
        q = _hits[key]
        while q and now - q[0] >= WINDOW:
            q.popleft()
        if len(q) >= limit:
            return False
        q.append(now)
        return True


def check(request: Request, player_id: str | None, limit: int, now: float | None = None) -> None:
    """Raise 429 if this player made `limit` requests in the last minute, or this IP
    `limit * IP_MULTIPLIER`."""
    now = time.monotonic() if now is None else now
    ip = request.client.host if request.client else "unknown"
    keys = [(f"ip:{ip}", limit * IP_MULTIPLIER)]
    if player_id:
        keys.append((f"player:{player_id}", limit))
    for key, key_limit in keys:
        if not _allow(key, key_limit, now):
            raise HTTPException(429, "Too many submissions. Wait a minute and try again.")


def reset() -> None:
    with _lock:
        _hits.clear()
