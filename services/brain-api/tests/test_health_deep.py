"""GET /health/deep — the completion probe is the arbiter of inference health.

The gateway's /models catalogue is informational: wildcard/passthrough
routing serves models the catalogue never lists, so absence must not
degrade. A completion that fails or answers garbage must.
"""

from __future__ import annotations

import httpx
import pytest


class _FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = ""

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("boom", request=None, response=None)  # type: ignore[arg-type]


class _FakeClient:
    """Stands in for httpx.AsyncClient inside health_deep."""

    def __init__(self, *, listed_models, completion_text):
        self.listed_models = listed_models
        self.completion_text = completion_text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get(self, url, **kwargs):
        if url.endswith("/health/deep"):
            return _FakeResponse(payload={"status": "ok", "checks": {}})
        if url.endswith("/models"):
            return _FakeResponse(payload={"data": [{"id": m} for m in self.listed_models]})
        raise AssertionError(f"unexpected GET {url}")

    async def post(self, url, **kwargs):
        assert url.endswith("/chat/completions"), url
        return _FakeResponse(
            payload={"choices": [{"message": {"content": self.completion_text}}]}
        )


@pytest.fixture()
def deep_env(vault, monkeypatch):
    monkeypatch.setenv("EMBEDDINGS_URL", "http://embeddings:8080")
    monkeypatch.setenv("EMBEDDINGS_API_KEY", "k")
    monkeypatch.setenv("INFERENCE_URL", "http://inference/v1")
    monkeypatch.setenv("INFERENCE_API_KEY", "k")
    monkeypatch.setenv("BRAIN_CLASSIFY_MODEL", "classify-1")
    return monkeypatch


def _call(client_app, monkeypatch, *, listed_models, completion_text):
    fake = _FakeClient(listed_models=listed_models, completion_text=completion_text)
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: fake)
    return client_app.get("/health/deep")


def test_pong_completion_is_healthy(deep_env, client):
    resp = _call(client, deep_env, listed_models=["classify-1"], completion_text="pong")
    body = resp.json()
    assert body["status"] == "ok"
    inf = body["checks"]["inference"]
    assert inf["completion"]["ok"] is True
    assert inf["completion"]["response"] == "pong"


def test_unlisted_model_with_working_completion_is_healthy(deep_env, client):
    """Wildcard routing: model absent from /models but serving fine."""
    resp = _call(client, deep_env, listed_models=["something-else"], completion_text="pong")
    body = resp.json()
    assert body["status"] == "ok"
    inf = body["checks"]["inference"]
    assert inf["classify_model_available"] is False
    assert inf["completion"]["ok"] is True


def test_garbage_completion_degrades(deep_env, client):
    resp = _call(client, deep_env, listed_models=["classify-1"], completion_text="banana")
    body = resp.json()
    assert body["status"] == "degraded"
    assert body["checks"]["inference"]["completion"]["ok"] is False
