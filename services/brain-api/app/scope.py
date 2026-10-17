"""Brain scope: a worker token names one brain, and a query from another brain is denied.

BRAIN_ID names this brain. KB_WORKER_TOKENS lists worker tokens as
``brain_id=token`` pairs, comma separated. Router tokens (KB_ROUTER_TOKENS)
name no brain and stay unscoped.
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from collections import deque

DENIALS_KEPT = int(os.getenv("BRAIN_DENIALS_KEPT", "1000"))


def _load_worker_tokens() -> dict[str, str]:
    found = {}
    for pair in os.getenv("KB_WORKER_TOKENS", "").split(","):
        brain, sep, token = pair.strip().partition("=")
        if sep and brain.strip() and token.strip():
            found[token.strip()] = brain.strip()
    return found


BRAIN_ID = os.getenv("BRAIN_ID", "").strip()
WORKER_TOKENS = _load_worker_tokens()

_denials: deque[dict] = deque(maxlen=DENIALS_KEPT)
_lock = threading.Lock()


def worker_brain(token: str) -> str | None:
    return WORKER_TOKENS.get(token)


def record_denial(requester_scope: str, query: str, session_title: str) -> dict:
    row = {
        "denial_id": f"bdn-{uuid.uuid4().hex}",
        "at_ms": int(time.time() * 1000),
        "requester_scope": requester_scope,
        "target_brain": BRAIN_ID,
        "session_title": session_title,
        "query": query,
    }
    with _lock:
        _denials.append(row)
    return row


def denials_since(since_ms: int) -> list[dict]:
    with _lock:
        return [dict(row) for row in _denials if row["at_ms"] >= since_ms]
