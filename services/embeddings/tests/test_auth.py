import subprocess
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

GOOD = "good-key"
PROTECTED = [
    ("post", "/embed", {"key": "k", "content": "text", "producer": "p"}),
    ("post", "/search", {"query": "text"}),
    ("post", "/prune", {"producer": "p", "keep_keys": []}),
    ("get", "/stats", None),
    ("get", "/by-key/k", None),
    ("get", "/health/deep", None),
]
CHART = Path(__file__).resolve().parents[3] / "helm" / "embeddings"


@pytest.fixture()
def client(monkeypatch):
    import db
    import embed
    import main

    monkeypatch.setattr(embed, "is_configured", lambda: True)
    monkeypatch.setattr(embed, "embed_content", lambda _c: [{"embedding": [0.0, 0.0]}])
    monkeypatch.setattr(embed, "embed_text", lambda _t, model=None: [0.0, 0.0])
    monkeypatch.setattr(db, "upsert_chunks", lambda **_k: 1)
    monkeypatch.setattr(db, "search", lambda **_k: [{"key": "secret-row"}])
    monkeypatch.setattr(db, "prune", lambda **_k: {"deleted": 0, "kept": 0, "scanned": 0})
    monkeypatch.setattr(
        db, "get_producer_stats", lambda: {"producers": [], "total_rows": 0, "total_keys": 0}
    )
    monkeypatch.setattr(db, "get_by_key", lambda _k: [{"chunk_index": 0}])
    monkeypatch.setattr(db, "get_vector_count", lambda: 7)
    monkeypatch.setattr(db, "get_schema_dim", lambda *a, **k: 2)
    return TestClient(main.app)


def _call(client, method, path, body, token=None):
    headers = {"Authorization": f"Bearer {token}"} if token is not None else {}
    if body is None:
        return getattr(client, method)(path, headers=headers)
    return getattr(client, method)(path, json=body, headers=headers)


@pytest.mark.parametrize("mode", [None, "required"])
@pytest.mark.parametrize("keys", [None, "", "   ", ",", "good-key,", "good key", "a,,b"])
@pytest.mark.parametrize(("method", "path", "body"), PROTECTED)
def test_required_mode_refuses_without_valid_key_configuration(
    client, monkeypatch, mode, keys, method, path, body
):
    if mode is None:
        monkeypatch.delenv("AUTH_MODE", raising=False)
    else:
        monkeypatch.setenv("AUTH_MODE", mode)
    if keys is None:
        monkeypatch.delenv("API_KEYS", raising=False)
    else:
        monkeypatch.setenv("API_KEYS", keys)

    for token in (None, GOOD, "good", "a", "b", ""):
        response = _call(client, method, path, body, token)
        assert response.status_code == 503
        assert response.json() == {
            "detail": (
                "embeddings refuses protected operations: API_KEYS holds no valid "
                "accepted keys. Set API_KEYS to comma-separated keys, or select "
                "AUTH_MODE=local for an unauthenticated local deployment."
            )
        }


@pytest.mark.parametrize(("method", "path", "body"), PROTECTED)
def test_required_mode_rejects_missing_and_wrong_keys(client, monkeypatch, method, path, body):
    monkeypatch.setenv("AUTH_MODE", "required")
    monkeypatch.setenv("API_KEYS", f"other-key, {GOOD}")

    missing = _call(client, method, path, body)
    assert missing.status_code == 401
    assert missing.json() == {"detail": "missing bearer token"}

    wrong = _call(client, method, path, body, "wrong-key")
    assert wrong.status_code == 401
    assert wrong.json() == {"detail": "Invalid API key"}

    raw = client.post(path, json=body, headers={"Authorization": GOOD})
    assert raw.status_code == 401
    assert raw.json() == {"detail": "missing bearer token"}

    assert _call(client, method, path, body, GOOD).status_code == 200
    assert _call(client, method, path, body, "other-key").status_code == 200


def test_unknown_mode_refuses(client, monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "open")
    monkeypatch.setenv("API_KEYS", GOOD)

    response = _call(client, "post", "/search", {"query": "text"}, GOOD)

    assert response.status_code == 503
    assert response.json() == {
        "detail": "embeddings refuses protected operations: unsupported AUTH_MODE 'open', use required or local."
    }


@pytest.mark.parametrize("keys", [None, "", GOOD])
@pytest.mark.parametrize(("method", "path", "body"), PROTECTED)
def test_local_mode_serves_without_a_key(client, monkeypatch, keys, method, path, body):
    monkeypatch.setenv("AUTH_MODE", " Local ")
    if keys is None:
        monkeypatch.delenv("API_KEYS", raising=False)
    else:
        monkeypatch.setenv("API_KEYS", keys)

    assert _call(client, method, path, body).status_code == 200


@pytest.mark.parametrize(
    ("mode", "keys", "expected"),
    [
        (None, None, {"mode": "required", "keys_configured": False}),
        ("required", "", {"mode": "required", "keys_configured": False}),
        ("required", "a,,b", {"mode": "required", "keys_configured": False}),
        ("required", GOOD, {"mode": "required", "keys_configured": True}),
        ("local", None, {"mode": "local", "keys_configured": False}),
        ("open", GOOD, {"mode": "open", "keys_configured": True}),
    ],
)
def test_health_reports_auth_state_without_data(client, monkeypatch, mode, keys, expected):
    if mode is None:
        monkeypatch.delenv("AUTH_MODE", raising=False)
    else:
        monkeypatch.setenv("AUTH_MODE", mode)
    if keys is None:
        monkeypatch.delenv("API_KEYS", raising=False)
    else:
        monkeypatch.setenv("API_KEYS", keys)

    for headers in ({}, {"Authorization": "Bearer wrong-key"}):
        response = client.get("/health", headers=headers)
        assert response.status_code == 200
        body = response.json()
        assert body["auth"] == expected
        assert "secret-row" not in response.text
        assert set(body) == {
            "status",
            "embedding_model",
            "vector_count",
            "schema_dim",
            "embedding_configured",
            "auth",
        }


def test_helm_chart_renders_required_mode_and_the_accepted_keys_secret():
    rendered = subprocess.run(
        ["helm", "template", "embeddings", str(CHART)], check=True, capture_output=True, text=True
    ).stdout
    statefulset = next(
        doc for doc in yaml.safe_load_all(rendered) if doc and doc["kind"] == "StatefulSet"
    )
    container = statefulset["spec"]["template"]["spec"]["containers"][0]

    assert {"name": "AUTH_MODE", "value": "required"} in container["env"]
    assert container["envFrom"] == [{"secretRef": {"name": "embeddings-secrets"}}]
