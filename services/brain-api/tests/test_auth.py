"""Auth fails closed.

An empty token set used to return early from require_token, which meant the
vault, the feed and the marker endpoints all answered unauthenticated. A
missing credential is a misconfiguration, not a decision to be public — and
init and install always generate a bearer, so an empty set is never
intentional.
"""

from __future__ import annotations

import importlib
from pathlib import Path

from fastapi.testclient import TestClient

_MODULES = [
    "app.feed",
    "app.signal",
    "app.markers",
    "app.tick_trigger",
    "app.pipeline",
    "app.main",
]


def _client_with(monkeypatch, tmp_path: Path, bearer: str) -> TestClient:
    from agentibrain.scaffold import scaffold as run_scaffold

    run_scaffold(tmp_path)
    monkeypatch.setenv("VAULT_ROOT", str(tmp_path))
    monkeypatch.setenv("KB_ROUTER_TOKENS", bearer)
    monkeypatch.setenv("KB_ROUTER_TOKEN", bearer)
    monkeypatch.setenv("FEED_CACHE_TTL_SECONDS", "0")
    for name in _MODULES:
        if name in importlib.sys.modules:
            importlib.reload(importlib.sys.modules[name])
        else:
            importlib.import_module(name)
    from app.main import app

    return TestClient(app)


def test_an_unconfigured_brain_refuses_to_serve(monkeypatch, tmp_path):
    client = _client_with(monkeypatch, tmp_path, "")

    response = client.get("/feed")

    assert response.status_code == 503
    assert "KB_ROUTER_TOKEN" in response.text


def test_a_wrong_bearer_is_rejected(monkeypatch, tmp_path):
    client = _client_with(monkeypatch, tmp_path, "right")

    assert client.get("/feed", headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_a_missing_header_is_rejected(monkeypatch, tmp_path):
    client = _client_with(monkeypatch, tmp_path, "right")

    assert client.get("/feed").status_code == 401


def test_the_configured_bearer_is_accepted(monkeypatch, tmp_path):
    client = _client_with(monkeypatch, tmp_path, "right")

    assert client.get("/feed", headers={"Authorization": "Bearer right"}).status_code == 200
