"""brain_tick reports an index failure as retryable, never as completed."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

_APP = Path(__file__).resolve().parents[1] / "app"
if str(_APP) not in sys.path:
    sys.path.insert(0, str(_APP))

from tools import tick  # noqa: E402


class _Registry:
    def __init__(self):
        self.tools = {}

    def tool(self, *a, **k):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn

        return deco


def _run(monkeypatch, status_payload: dict) -> dict:
    async def fake_http(method, url, headers=None, params=None, body=None, timeout=10):
        if method == "POST":
            return json.dumps({"job_id": "abc123", "status": "pending"})
        return json.dumps(status_payload)

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(tick, "http_request", fake_http)
    monkeypatch.setattr(tick.asyncio, "sleep", no_sleep)
    reg = _Registry()
    tick.register(reg)
    return json.loads(asyncio.run(reg.tools["brain_tick"](timeout=6)))


def test_pending_request_with_index_error_is_retryable(monkeypatch):
    result = _run(
        monkeypatch,
        {"status": "pending", "attempts": 1, "last_error": "embed_arcs.py failed"},
    )

    assert result["status"] == "pending"
    assert result["retryable"] is True
    assert result["last_error"] == "embed_arcs.py failed"
    assert "indexing failed" in result["note"]


def test_completed_request_is_not_retryable(monkeypatch):
    result = _run(monkeypatch, {"status": "completed"})

    assert result["status"] == "completed"
    assert "retryable" not in result
