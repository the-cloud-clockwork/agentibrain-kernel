"""GET /stats — per-producer index coverage.

Exists so a caller can tell "this producer holds no rows" apart from "this
query matched nothing". Search cannot express that difference: both come back
as an empty result list, which is why an unindexed producer stays invisible
until someone counts.

Unlike /health/deep this spends no embedding call, so it is safe to poll.
"""

import importlib
import os

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


# ---------------------------------------------------------------------------
# The query itself. Everything above stubs `get_producer_stats` and therefore
# proves only that the route forwards it — the SQL would pass those tests
# deleted. Same DSN gate and destructive-fixture pattern as
# test_reconcile_dim.py: never POSTGRES_URL, which points at a live stack.
# ---------------------------------------------------------------------------

psycopg = pytest.importorskip("psycopg")

DSN = os.environ.get("EMBED_TEST_POSTGRES_URL", "")


@pytest.fixture()
def seeded_db(monkeypatch):
    if not DSN:
        pytest.skip("EMBED_TEST_POSTGRES_URL not set (destructive DB tests)")
    try:
        with psycopg.connect(DSN, connect_timeout=3) as conn:
            conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
            conn.execute("DROP TABLE IF EXISTS content_embeddings")
            conn.commit()
    except psycopg.OperationalError as exc:
        pytest.skip(f"Postgres unreachable: {exc}")

    monkeypatch.setenv("POSTGRES_URL", DSN)
    import db
    import embed

    embed = importlib.reload(embed)
    db = importlib.reload(db)
    db._pool = None
    dim = embed.target_dim()
    vec = str([0.0] * dim)

    with db.get_pool().connection() as conn:
        # brain-arc: two documents, one of them chunked — so `keys` and `rows`
        # genuinely disagree and a keys/rows mix-up cannot pass unnoticed.
        for key, idx in (("arc-a", 0), ("arc-a", 1), ("arc-b", 0)):
            conn.execute(
                "INSERT INTO content_embeddings (key, chunk_idx, producer, content_type,"
                " text_preview, embedding) VALUES (%s,%s,'brain-arc','arc','x',%s::vector)",
                (key, idx, vec),
            )
        # A NULL created_at, which MAX() returns as NULL for a single-row group
        # — the shape that makes an unguarded `.isoformat()` raise.
        conn.execute(
            "INSERT INTO content_embeddings (key, chunk_idx, producer, content_type,"
            " text_preview, embedding, created_at)"
            " VALUES ('less-a',0,'brain-lesson','lesson-log','y',%s::vector, NULL)",
            (vec,),
        )
        conn.commit()
    yield db
    if db._pool is not None:
        db._pool.close()
        db._pool = None


def test_the_real_query_separates_keys_from_chunk_rows(seeded_db):
    stats = seeded_db.get_producer_stats()
    by_name = {p["producer"]: p for p in stats["producers"]}

    assert by_name["brain-arc"]["keys"] == 2, "two documents"
    assert by_name["brain-arc"]["rows"] == 3, "three chunks across them"
    assert stats["total_rows"] == 4
    assert stats["total_keys"] == 3


def test_the_real_query_survives_a_null_created_at(seeded_db):
    """`MAX(created_at)` is NULL for an all-NULL group; `.isoformat()` on that
    would raise AttributeError and take the endpoint down with it."""
    by_name = {p["producer"]: p for p in seeded_db.get_producer_stats()["producers"]}
    assert by_name["brain-lesson"]["newest"] is None
    assert by_name["brain-arc"]["newest"] is not None
