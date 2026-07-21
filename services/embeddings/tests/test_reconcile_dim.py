"""The destructive path: reconciling the pgvector column dimension. These are
the tests that guard against silently wiping the vault's memory on a restart.

These tests DROP and recreate content_embeddings, so they run ONLY against a
DSN named explicitly for testing — EMBED_TEST_POSTGRES_URL. They deliberately
do NOT fall back to POSTGRES_URL: that variable points at the live embeddings
database in a running stack, and a fixture that drops its table would wipe
real vectors. Skipped when EMBED_TEST_POSTGRES_URL is unset.
"""

import importlib
import os

import pytest

psycopg = pytest.importorskip("psycopg")

DSN = os.environ.get("EMBED_TEST_POSTGRES_URL", "")

pytestmark = pytest.mark.skipif(
    not DSN, reason="EMBED_TEST_POSTGRES_URL not set (destructive DB tests)"
)


@pytest.fixture()
def fresh_table(monkeypatch):
    """A content_embeddings table dropped clean before each test, plus a db
    module bound to the test DSN. Returns (db, embed)."""
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
    yield db, embed
    if db._pool is not None:
        db._pool.close()
        db._pool = None


def _seed(db, dim, n):
    """Insert n rows of dimension `dim` directly, bypassing the model."""
    pool = db.get_pool()
    with pool.connection() as conn:
        for i in range(n):
            vec = "[" + ",".join(["0.1"] * dim) + "]"
            conn.execute(
                """INSERT INTO content_embeddings (key, chunk_idx, producer, embedding)
                   VALUES (%s, 0, 'test', %s::vector)""",
                (f"k{i}", vec),
            )
        conn.commit()


def test_empty_table_wrong_dim_migrates_in_place(fresh_table, monkeypatch):
    """No rows to lose → alter the column automatically."""
    db, embed = fresh_table
    monkeypatch.setenv("LLM_EMBED_MODEL", "text-embedding-3-small")  # 1536
    embed = importlib.reload(embed)
    db.get_pool()  # creates table at 1536
    assert db.get_schema_dim() == 1536

    # Now the model is 3-large (3072); reconcile on an empty table.
    monkeypatch.setenv("LLM_EMBED_MODEL", "text-embedding-3-large")
    importlib.reload(embed)
    db._reconcile_dim(db.get_pool(), 3072)
    assert db.get_schema_dim() == 3072


def test_populated_table_wrong_dim_refuses_without_flag(fresh_table, monkeypatch):
    """The data-loss guard: rows present + dim mismatch + no force flag →
    schema untouched, rows preserved. Loud failure, not a silent wipe."""
    db, embed = fresh_table
    monkeypatch.setenv("LLM_EMBED_MODEL", "text-embedding-3-small")  # 1536
    importlib.reload(embed)
    db.get_pool()
    _seed(db, 1536, 3)

    monkeypatch.delenv("EMBED_DIM_FORCE_MIGRATE", raising=False)
    db._reconcile_dim(db.get_pool(), 3072)

    assert db.get_schema_dim() == 1536  # unchanged
    assert db.get_vector_count() == 3  # rows preserved


def test_populated_table_migrates_with_force_flag(fresh_table, monkeypatch):
    """With the operator's explicit opt-in, the rows are dropped and the
    column is migrated."""
    db, embed = fresh_table
    monkeypatch.setenv("LLM_EMBED_MODEL", "text-embedding-3-small")
    importlib.reload(embed)
    db.get_pool()
    _seed(db, 1536, 3)

    monkeypatch.setenv("EMBED_DIM_FORCE_MIGRATE", "true")
    db._reconcile_dim(db.get_pool(), 3072)

    assert db.get_schema_dim() == 3072
    assert db.get_vector_count() == 0
