"""GET /stats — per-producer index coverage.

Exists so a caller can tell "this producer holds no rows" apart from "this
query matched nothing". Search cannot express that difference: both come back
as an empty result list, which is why an unindexed producer stays invisible
until someone counts.

Unlike /health/deep this spends no embedding call, so it is safe to poll.
"""

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("API_KEYS", "")  # disable auth (any token passes)
    import main

    return TestClient(main.app, headers={"Authorization": "Bearer test"})


def test_reports_each_producer(client, monkeypatch):
    import db

    monkeypatch.setattr(
        db,
        "get_producer_stats",
        lambda: {
            "producers": [
                {"producer": "brain-arc", "keys": 12, "rows": 48, "newest": "2026-08-12T04:00:00"},
                {"producer": "brain-lesson", "keys": 3, "rows": 9, "newest": None},
            ],
            "total_rows": 57,
            "total_keys": 15,
        },
    )
    body = client.get("/stats").json()
    assert body["total_keys"] == 15
    assert {p["producer"] for p in body["producers"]} == {"brain-arc", "brain-lesson"}


def test_empty_index_is_an_answer_not_an_error(client, monkeypatch):
    """A brand-new deployment has no rows. That is a fact to report, not a 500."""
    import db

    monkeypatch.setattr(
        db, "get_producer_stats", lambda: {"producers": [], "total_rows": 0, "total_keys": 0}
    )
    resp = client.get("/stats")
    assert resp.status_code == 200
    assert resp.json()["producers"] == []


def test_db_failure_surfaces_as_500(client, monkeypatch):
    import db

    def _boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(db, "get_producer_stats", _boom)
    assert client.get("/stats").status_code == 500
