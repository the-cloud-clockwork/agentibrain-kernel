"""Exact project filter and metadata-only updates.

The DB-backed tests use EMBED_TEST_POSTGRES_URL (pgvector in CI) and skip when
it is unset; they drop and recreate content_embeddings, so never point them at
a live stack.
"""

import importlib
import json
import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "brain-ops"
    / "tests"
    / "fixtures"
    / "sv2_brp03_knowledge.json"
)
SETS = json.loads(FIXTURE.read_text(encoding="utf-8"))["sets"]


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "local")
    import embed
    import main

    monkeypatch.setattr(embed, "is_configured", lambda: True)
    monkeypatch.setattr(embed, "embed_text", lambda _t, model=None: [0.0, 0.0])
    return TestClient(main.app, headers={"Authorization": "Bearer test"})


def test_search_forwards_the_exact_project_filter(client, monkeypatch):
    import db

    seen = {}

    def fake_search(**kwargs):
        seen.update(kwargs)
        return []

    monkeypatch.setattr(db, "search", fake_search)
    resp = client.post("/search", json={"query": "q", "project_id": "org/repo"})

    assert resp.status_code == 200
    assert seen["project_id"] == "org/repo"


def test_search_without_a_project_filter_is_unchanged(client, monkeypatch):
    import db

    seen = {}
    monkeypatch.setattr(db, "search", lambda **kw: seen.update(kw) or [])
    client.post("/search", json={"query": "q"})

    assert seen["project_id"] is None


def test_metadata_update_reports_a_missing_key(client, monkeypatch):
    import db

    monkeypatch.setattr(db, "update_metadata", lambda key, producer, metadata: 0)
    resp = client.post(
        "/metadata", json={"key": "nope", "producer": "p", "metadata": {"status": "current"}}
    )

    assert resp.status_code == 404
    assert resp.json() == {"detail": "key not found: nope"}


def test_metadata_update_returns_the_rows_it_changed(client, monkeypatch):
    import db

    calls = []
    monkeypatch.setattr(
        db, "update_metadata", lambda key, producer, metadata: calls.append((key, producer)) or 2
    )
    resp = client.post(
        "/metadata", json={"key": "k", "producer": "p", "metadata": {"status": "superseded"}}
    )

    assert resp.status_code == 200
    assert resp.json() == {"key": "k", "rows_updated": 2}
    assert calls == [("k", "p")]


def test_metadata_update_requires_a_producer(client):
    resp = client.post("/metadata", json={"key": "k", "metadata": {}})

    assert resp.status_code == 422


psycopg = pytest.importorskip("psycopg")

DSN = os.environ.get("EMBED_TEST_POSTGRES_URL", "")


@pytest.fixture()
def fresh_db(monkeypatch):
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
    db.get_pool()
    yield db, embed.target_dim()
    if db._pool is not None:
        db._pool.close()
        db._pool = None


def _vector(dim: int) -> list[float]:
    return [1.0] + [0.0] * (dim - 1)


def _seed(db, dim: int, key: str, projects: list[str], project_id: str) -> None:
    db.upsert_chunks(
        key=key,
        producer="brain-knowledge",
        content_type="lesson",
        chunks=[
            {
                "chunk_idx": 0,
                "text_preview": f"text of {key}",
                "embedding": _vector(dim),
                "metadata": {"project_id": project_id, "projects": projects, "kind": "lesson"},
            }
        ],
    )


def _snapshot(db) -> list[tuple]:
    with db.get_pool().connection() as conn:
        return conn.execute(
            "SELECT key, chunk_idx, metadata::text, embedding::text FROM content_embeddings"
            " ORDER BY key, chunk_idx"
        ).fetchall()


@pytest.mark.parametrize("fx", SETS, ids=[s["name"] for s in SETS])
def test_a_substring_of_another_repository_never_passes_the_project_filter(fresh_db, fx):
    db, dim = fresh_db
    own = fx["late_lesson"]["project_id"]
    foreign = fx["foreign_project"]
    _seed(db, dim, "own", [own], own)
    _seed(db, dim, "foreign", [foreign], foreign)
    _seed(db, dim, "unknown", [], "unknown")
    before = _snapshot(db)

    exact = db.search(_vector(dim), project_id=own, limit=10)
    substring = db.search(_vector(dim), project_id=fx["substring_probe"], limit=10)
    unknown = db.search(_vector(dim), project_id="unknown", limit=10)
    unfiltered = db.search(_vector(dim), limit=10)

    assert [r["key"] for r in exact] == ["own"]
    assert exact[0]["metadata"]["project_id"] == own
    assert substring == []
    assert unknown == []
    assert {r["key"] for r in unfiltered} == {"own", "foreign", "unknown"}
    assert _snapshot(db) == before
    corrected = db.search(_vector(dim), project_id=own, limit=10)
    assert [r["key"] for r in corrected] == ["own"]


def test_the_project_filter_has_its_own_index(fresh_db):
    db, _ = fresh_db
    with db.get_pool().connection() as conn:
        rows = conn.execute(
            "SELECT indexdef FROM pg_indexes WHERE tablename = 'content_embeddings'"
            " AND indexname = 'idx_ce_projects'"
        ).fetchall()

    assert len(rows) == 1
    assert "projects" in rows[0][0]


def test_a_metadata_update_leaves_the_embedding_and_text_alone(fresh_db):
    db, dim = fresh_db
    _seed(db, dim, "adr", ["org/repo"], "org/repo")
    _seed(db, dim, "other", ["org/repo"], "org/repo")
    with db.get_pool().connection() as conn:
        before = conn.execute(
            "SELECT embedding::text, text_preview FROM content_embeddings WHERE key = 'adr'"
        ).fetchone()
    untouched = [row for row in _snapshot(db) if row[0] == "other"]

    meta = {"project_id": "org/repo", "projects": ["org/repo"], "status": "superseded"}
    assert db.update_metadata("adr", "brain-arc", meta) == 0
    changed = db.update_metadata("adr", "brain-knowledge", meta)

    assert changed == 1
    with db.get_pool().connection() as conn:
        after = conn.execute(
            "SELECT embedding::text, text_preview, metadata FROM content_embeddings"
            " WHERE key = 'adr'"
        ).fetchone()
    assert after[:2] == before
    assert after[2]["status"] == "superseded"
    assert [row for row in _snapshot(db) if row[0] == "other"] == untouched
    assert db.update_metadata("missing", "brain-knowledge", {"status": "current"}) == 0


def test_the_brain_filter_keeps_another_brain_out(fresh_db):
    db, dim = fresh_db
    for key, brain in (("swarm-item", "swarm"), ("personal-item", "personal")):
        db.upsert_chunks(
            key=key,
            producer="brain-knowledge",
            content_type="lesson",
            chunks=[
                {
                    "chunk_idx": 0,
                    "text_preview": key,
                    "embedding": _vector(dim),
                    "metadata": {
                        "project_id": "org/repo",
                        "projects": ["org/repo"],
                        "brain": brain,
                    },
                }
            ],
        )

    hits = db.search(_vector(dim), project_id="org/repo", brain="swarm", limit=10)

    assert [r["key"] for r in hits] == ["swarm-item"]


def _brain_ops_indexer():
    path = str(Path(__file__).resolve().parents[2] / "brain-ops")
    if path not in sys.path:
        sys.path.insert(0, path)
    return importlib.import_module("knowledge_index")


@pytest.mark.parametrize("fx", SETS, ids=[s["name"] for s in SETS])
def test_a_late_lesson_from_the_indexer_is_found_under_its_project(fresh_db, fx):
    db, dim = fresh_db
    knowledge_index = _brain_ops_indexer()
    late = fx["late_lesson"]
    projects = fx["filler"]["projects"]
    entries = [
        f"## {fx['log_date']}T00:00:{n % 60:02d}+00:00 — filler <!-- @provenance "
        f'marker_id="mk-{n:032d}" project_id="{projects[n % len(projects)]}" -->\n\n'
        f"{fx['filler']['text'].format(n=n)}\n"
        for n in range(fx["filler"]["count"])
    ]
    entries.append(
        f'## {late["ts"]} — {late["source"]} <!-- @provenance marker_id="{late["marker_id"]}" '
        f'project_id="{late["project_id"]}" brain="{fx["brain"]}" -->\n\n{late["content"]}\n'
    )
    rel = f"left/reference/lessons-{fx['log_date']}.md"
    items = knowledge_index.lesson_items(rel, "\n".join(entries))
    for item in items:
        db.upsert_chunks(
            key=item.key,
            producer=knowledge_index.PRODUCER,
            content_type=item.kind,
            chunks=[
                {
                    "chunk_idx": 0,
                    "text_preview": item.content[:1000],
                    "embedding": _vector(dim),
                    "metadata": item.metadata,
                }
            ],
        )

    hits = db.search(_vector(dim), project_id=late["project_id"], brain=fx["brain"], limit=100)

    assert [h["key"] for h in hits] == [f"lesson:{late['marker_id']}"]
    assert hits[0]["metadata"]["source"]["path"] == rel
    assert hits[0]["metadata"]["source"]["entry_ts"] == late["ts"]
    assert late["content"] in hits[0]["text_preview"]
