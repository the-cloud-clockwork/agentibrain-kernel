"""API key authentication."""

import os

from fastapi import Header, HTTPException

REQUIRED = "required"
LOCAL = "local"
MODES = (REQUIRED, LOCAL)


def auth_mode() -> str:
    return os.environ.get("AUTH_MODE", "").strip().lower() or REQUIRED


def accepted_keys() -> frozenset[str]:
    entries = [entry.strip() for entry in os.environ.get("API_KEYS", "").split(",")]
    if any(not entry or any(char.isspace() for char in entry) for entry in entries):
        return frozenset()
    return frozenset(entries)


def auth_status() -> dict:
    return {"mode": auth_mode(), "keys_configured": bool(accepted_keys())}


async def require_api_key(authorization: str | None = Header(None)) -> None:
    mode = auth_mode()
    if mode == LOCAL:
        return
    if mode not in MODES:
        raise HTTPException(
            status_code=503,
            detail=f"embeddings refuses protected operations: unsupported AUTH_MODE {mode!r}, use required or local.",
        )
    keys = accepted_keys()
    if not keys:
        raise HTTPException(
            status_code=503,
            detail=(
                "embeddings refuses protected operations: API_KEYS holds no valid "
                "accepted keys. Set API_KEYS to comma-separated keys, or select "
                "AUTH_MODE=local for an unauthenticated local deployment."
            ),
        )
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    if authorization.removeprefix("Bearer ").strip() not in keys:
        raise HTTPException(status_code=401, detail="Invalid API key")
