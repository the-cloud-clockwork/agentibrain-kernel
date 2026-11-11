"""Synthesis calls charged to this brain's inference budget, for callers such as the maintenance tick."""

from __future__ import annotations

import os

import httpx
from pydantic import BaseModel, ConfigDict, Field

from . import brain_policy

INFERENCE_TOKEN_ENV = "INFERENCE_API_KEY"
TIMEOUT_SECONDS = float(os.getenv("BRAIN_LLM_TIMEOUT_SECONDS", "600"))


class Request(BaseModel):
    model_config = ConfigDict(extra="forbid")
    brain_id: str = Field(min_length=1, max_length=128)
    model: str = Field(min_length=1)
    prompt: str = Field(min_length=1)
    max_tokens: int = Field(ge=1)


class Refused(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


async def _complete(url: str, model: str, prompt: str, max_tokens: int) -> str:
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0.3,
    }
    headers = {"Content-Type": "application/json"}
    token = os.environ.get(INFERENCE_TOKEN_ENV, "")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
            resp = await client.post(
                f"{url.rstrip('/')}/chat/completions", json=body, headers=headers
            )
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"] or ""
    except (httpx.HTTPError, KeyError, IndexError, ValueError) as exc:
        raise Refused(502, f"inference gateway failed: {type(exc).__name__}") from exc


async def synthesize(request: Request) -> dict:
    """Reserve, call the gateway and release; raises Refused without calling it when refused."""
    try:
        current = brain_policy.active()
    except brain_policy.PolicyRefused as exc:
        brain_policy.record_refusal(exc.reason)
        raise Refused(503, "policy_refused") from exc
    if request.brain_id != current.brain_id:
        raise Refused(403, f"synthesis names brain {request.brain_id}, not this brain")
    url = os.getenv("INFERENCE_URL", "")
    if not url:
        raise Refused(503, "inference gateway not configured")
    lease = brain_policy.budget.reserve(current)
    if lease is None:
        raise Refused(429, "inference_budget_spent")
    try:
        tokens = min(request.max_tokens, current.policy.synthesis_max_tokens)
        content = await _complete(url, request.model, request.prompt, tokens)
    finally:
        brain_policy.budget.release(lease)
    return {"brain_id": current.brain_id, "policy_revision": current.revision, "content": content}
