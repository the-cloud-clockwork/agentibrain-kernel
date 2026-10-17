"""A worker token names one brain; a query from another brain's token is denied and recorded."""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_MODULES = [
    "app.feed",
    "app.signal",
    "app.markers",
    "app.tick_trigger",
    "app.pipeline",
    "app.scope",
    "app.main",
]
ROUTER = "router-bearer"


def _client(monkeypatch, tmp_path: Path) -> TestClient:
    from agentibrain.scaffold import scaffold as run_scaffold

    run_scaffold(tmp_path)
    monkeypatch.setenv("VAULT_ROOT", str(tmp_path))
    monkeypatch.setenv("KB_ROUTER_TOKENS", ROUTER)
    monkeypatch.setenv("FEED_CACHE_TTL_SECONDS", "0")
    monkeypatch.setenv("BRAIN_ID", "hive-a")
    monkeypatch.setenv("KB_WORKER_TOKENS", "hive-a=worker-a, hive-b=worker-b")
    for name in _MODULES:
        if name in importlib.sys.modules:
            importlib.reload(importlib.sys.modules[name])
        else:
            importlib.import_module(name)
    from app.main import app

    return TestClient(app)


def _denials(client: TestClient, since_ms: int = 0) -> list[dict]:
    response = client.get(
        "/denials", params={"since_ms": since_ms}, headers={"Authorization": f"Bearer {ROUTER}"}
    )
    assert response.status_code == 200
    return response.json()["denials"]


def test_a_same_scope_query_is_allowed_and_not_recorded(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)

    response = client.get(
        "/vault/search", params={"q": "x"}, headers={"Authorization": "Bearer worker-a"}
    )

    assert response.status_code == 200
    assert _denials(client) == []


def test_a_cross_scope_query_is_denied_and_recorded(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)

    response = client.get(
        "/vault/search",
        params={"q": "x"},
        headers={"Authorization": "Bearer worker-b", "X-Session-Title": "fix the feed"},
    )

    assert response.status_code == 403
    assert "hive-b" in response.text
    [row] = _denials(client)
    assert set(row) == {
        "denial_id",
        "at_ms",
        "requester_scope",
        "target_brain",
        "session_title",
        "query",
    }
    assert row["requester_scope"] == "hive-b"
    assert row["target_brain"] == "hive-a"
    assert row["session_title"] == "fix the feed"
    assert row["query"] == "/vault/search"
    assert row["at_ms"] > 0


def test_each_denial_is_recorded_once(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    for _ in range(2):
        client.get("/feed", headers={"Authorization": "Bearer worker-b"})

    rows = _denials(client)

    assert len(rows) == 2
    assert rows[0]["denial_id"] != rows[1]["denial_id"]
    assert _denials(client, rows[1]["at_ms"] + 1) == []


def test_a_worker_token_cannot_read_denials(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)

    assert client.get("/denials", headers={"Authorization": "Bearer worker-a"}).status_code == 403


def test_a_cross_scope_probe_of_denials_is_recorded(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)

    assert client.get("/denials", headers={"Authorization": "Bearer worker-b"}).status_code == 403
    [row] = _denials(client)
    assert row["query"] == "/denials"


def test_router_tokens_stay_unscoped(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)

    assert client.get("/feed", headers={"Authorization": f"Bearer {ROUTER}"}).status_code == 200
    assert _denials(client) == []


def test_worker_tokens_without_a_brain_id_refuse_to_start(monkeypatch):
    monkeypatch.setenv("KB_WORKER_TOKENS", "hive-a=worker-a")
    monkeypatch.delenv("BRAIN_ID", raising=False)

    with pytest.raises(RuntimeError, match="BRAIN_ID"):
        importlib.reload(importlib.import_module("app.scope"))


def test_one_token_naming_two_brains_refuses_to_start(monkeypatch):
    monkeypatch.setenv("BRAIN_ID", "hive-a")
    monkeypatch.setenv("KB_WORKER_TOKENS", "hive-a=shared,hive-b=shared")

    with pytest.raises(RuntimeError, match="two brains"):
        importlib.reload(importlib.import_module("app.scope"))
