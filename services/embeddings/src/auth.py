"""API key authentication."""

import os
from fastapi import Header, HTTPException


API_KEYS = set(
    k.strip()
    for k in os.environ.get("API_KEYS", "").split(",")
    if k.strip()
)


async def require_api_key(authorization: str = Header(...)):
    if not API_KEYS:
        return
    token = authorization.removeprefix("Bearer ").strip()
    if token not in API_KEYS:
        raise HTTPException(status_code=401, detail="Invalid API key")
