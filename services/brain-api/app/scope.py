"""Brain scope: a worker token names one brain, and a query from another brain is denied.

BRAIN_ID names this brain. KB_WORKER_TOKENS lists worker tokens as
``brain_id=token`` pairs, comma separated; ``brain_id@projA|projB=token`` also
grants the token only those projects, and a ``#eng|ci`` suffix before ``=``
grants it only those lanes. Router tokens (KB_ROUTER_TOKENS)
name no brain and stay unscoped.
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from collections import deque

DENIALS_KEPT = int(os.getenv("BRAIN_DENIALS_KEPT", "1000"))
SESSION_TITLE_MAX = 200


def _load_worker_tokens() -> tuple[
    dict[str, str], dict[str, frozenset[str]], dict[str, frozenset[str]]
]:
    found: dict[str, str] = {}
    grants: dict[str, frozenset[str]] = {}
    roles: dict[str, frozenset[str]] = {}
    for pair in os.getenv("KB_WORKER_TOKENS", "").split(","):
        named, sep, token = pair.strip().partition("=")
        named, _, lanes = named.partition("#")
        brain, _, projects = named.partition("@")
        brain, token = brain.strip(), token.strip()
        if not (sep and brain and token):
            continue
        if found.get(token, brain) != brain:
            raise RuntimeError("KB_WORKER_TOKENS names one token for two brains")
        found[token] = brain
        grants[token] = frozenset(p.strip() for p in projects.split("|") if p.strip())
        roles[token] = frozenset(r.strip() for r in lanes.split("|") if r.strip())
    return found, grants, roles


BRAIN_ID = os.getenv("BRAIN_ID", "").strip()
WORKER_TOKENS, PROJECT_GRANTS, ROLE_GRANTS = _load_worker_tokens()
if WORKER_TOKENS and not BRAIN_ID:
    raise RuntimeError("KB_WORKER_TOKENS is set but BRAIN_ID is not; set BRAIN_ID and restart.")

_denials: deque[dict] = deque(maxlen=DENIALS_KEPT)
_lock = threading.Lock()


def worker_brain(token: str) -> str | None:
    return WORKER_TOKENS.get(token)


def project_grant(token: str) -> frozenset[str]:
    """Projects a worker token may write and read; empty means the whole brain."""
    return PROJECT_GRANTS.get(token, frozenset())


def role_grant(token: str) -> frozenset[str]:
    """Lanes a worker token may act as; empty means any lane."""
    return ROLE_GRANTS.get(token, frozenset())


def admits(grant: frozenset[str], claimed: set[str], *, unattributed: bool) -> bool:
    if not grant:
        return True
    if not claimed:
        return unattributed
    return claimed <= grant


def record_denial(requester_scope: str, query: str, session_title: str) -> None:
    row = {
        "denial_id": f"bdn-{uuid.uuid4().hex}",
        "at_ms": int(time.time() * 1000),
        "requester_scope": requester_scope,
        "target_brain": BRAIN_ID,
        "session_title": session_title[:SESSION_TITLE_MAX],
        "query": query,
    }
    with _lock:
        _denials.append(row)


def denials_since(since_ms: int) -> list[dict]:
    with _lock:
        return [dict(row) for row in _denials if row["at_ms"] >= since_ms]
