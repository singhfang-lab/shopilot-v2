"""In-process DuckDB data session store, shared between main.py and kb.py."""
from __future__ import annotations

import time

DATA_SESSIONS: dict[str, dict] = {}
DATA_SESSION_TTL = 3600  # 60 minutes


def get_session(key: str) -> dict | None:
    return DATA_SESSIONS.get(key)


def set_session(key: str, conn, schema: str, filenames: list[str]) -> None:
    DATA_SESSIONS[key] = {
        "conn": conn,
        "schema": schema,
        "filenames": filenames,
        "created_at": time.time(),
    }


def pop_session(key: str) -> dict | None:
    session = DATA_SESSIONS.pop(key, None)
    if session:
        try:
            session["conn"].close()
        except Exception:
            pass
    return session


def cleanup_expired() -> int:
    now = time.time()
    expired = [k for k, v in list(DATA_SESSIONS.items()) if now - v["created_at"] > DATA_SESSION_TTL]
    for k in expired:
        pop_session(k)
    return len(expired)
