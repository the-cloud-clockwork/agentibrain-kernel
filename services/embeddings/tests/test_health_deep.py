"""The /health/deep dim-match decision is the whole point of the feature —
it must report degraded exactly when an insert would fail, and never report
ok when it could not actually verify the schema."""

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("API_KEYS", "")  # disable auth (any token passes)
    import main

    # require_api_key declares Authorization as a required header, so send one
    # even though auth is disabled — otherwise FastAPI 422s before the handler.
    return TestClient(main.app, headers={"Authorization": "Bearer test"})


def _patch(monkeypatch, *, configured=True, model_dim=3072, schema_dim=3072, db_ok=True):
    import db
    import embed

    monkeypatch.setattr(embed, "is_configured", lambda: configured)
    monkeypatch.setattr(embed, "embed_text", lambda _t, model=None: [0.0] * model_dim)
    if db_ok:
        monkeypatch.setattr(db, "get_vector_count", lambda: 5)
        monkeypatch.setattr(db, "get_schema_dim", lambda *a, **k: schema_dim)
    else:
        def _boom(*a, **k):
            raise RuntimeError("db down")

        monkeypatch.setattr(db, "get_vector_count", _boom)
        monkeypatch.setattr(db, "get_schema_dim", _boom)


def test_ok_when_dims_match(client, monkeypatch):
    _patch(monkeypatch, model_dim=3072, schema_dim=3072)
    body = client.get("/health/deep").json()
    assert body["status"] == "ok"
    assert body["checks"]["embedding_api"]["dim_match"] is True


def test_degraded_on_dim_mismatch(client, monkeypatch):
    _patch(monkeypatch, model_dim=3072, schema_dim=1536)
    body = client.get("/health/deep").json()
    assert body["status"] == "degraded"
    assert body["checks"]["embedding_api"]["ok"] is False
    assert body["checks"]["embedding_api"]["dim_match"] is False


def test_unknown_schema_dim_is_not_a_pass(client, monkeypatch):
    """The false-green case: schema_dim unreadable must NOT report ok."""
    _patch(monkeypatch, model_dim=3072, schema_dim=None)
    body = client.get("/health/deep").json()
    assert body["status"] == "degraded"
    assert body["checks"]["embedding_api"]["ok"] is False


def test_degraded_when_db_down(client, monkeypatch):
    _patch(monkeypatch, db_ok=False)
    body = client.get("/health/deep").json()
    assert body["status"] == "degraded"
    assert body["checks"]["database"]["ok"] is False


def test_degraded_when_not_configured(client, monkeypatch):
    _patch(monkeypatch, configured=False)
    body = client.get("/health/deep").json()
    assert body["status"] == "degraded"
    assert body["checks"]["embedding_api"]["ok"] is False
