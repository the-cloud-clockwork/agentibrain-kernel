"""SV2-MUL-04: two brains on one Postgres server keep their knowledge apart.

The fixture creates two principals, each owning one schema on the shared test
server, and stores an arc with the same key and project in both. Misbinding
cases point a brain at the other's schema, or connect it through a principal
that can read another brain's claimed schema. Skipped without
EMBED_TEST_POSTGRES_URL, like the other destructive DB tests.
"""

from __future__ import annotations

import importlib
import os
import secrets

import pytest

psycopg = pytest.importorskip("psycopg")
from psycopg import sql  # noqa: E402
from psycopg.conninfo import conninfo_to_dict, make_conninfo  # noqa: E402

DSN = os.environ.get("EMBED_TEST_POSTGRES_URL", "")
BRAINS = ("personal", "swarm")
KEY = "arcs/2026-10-26-shared-arc.md"
PROJECT = "github.com/org/app"
LOGIN = secrets.token_urlsafe(12)

pytestmark = pytest.mark.skipif(
    not DSN, reason="EMBED_TEST_POSTGRES_URL not set (destructive DB tests)"
)


def _principal(brain: str) -> str:
    return f"mul04_{brain}"


def _dsn(principal: str, schema: str) -> str:
    base = conninfo_to_dict(DSN)
    base.update(user=principal, password=LOGIN, options=f"-csearch_path={schema},public")
    return make_conninfo("", **base)


def _admin(*statements: sql.Composable) -> None:
    with psycopg.connect(DSN, connect_timeout=3, autocommit=True) as conn:
        for statement in statements:
            conn.execute(statement)


def _drop() -> None:
    for brain in BRAINS:
        name = sql.Identifier(_principal(brain))
        _admin(
            sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(name),
            sql.SQL("DROP ROLE IF EXISTS {}").format(name),
        )


@pytest.fixture()
def server(monkeypatch):
    try:
        _admin(sql.SQL("CREATE EXTENSION IF NOT EXISTS vector"))
    except psycopg.OperationalError as exc:
        pytest.skip(f"Postgres unreachable: {exc}")
    _drop()
    for brain in BRAINS:
        name = sql.Identifier(_principal(brain))
        _admin(
            sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(name, sql.Literal(LOGIN)),
            sql.SQL("CREATE SCHEMA {} AUTHORIZATION {}").format(name, name),
        )
    opened = []

    def connect(brain: str, principal: str | None = None, schema: str | None = None):
        monkeypatch.setenv("BRAIN_ID", brain)
        monkeypatch.setenv(
            "POSTGRES_URL", _dsn(principal or _principal(brain), schema or _principal(brain))
        )
        import db
        import embed

        importlib.reload(embed)
        module = importlib.reload(db)
        module._pool = None
        opened.append(module)
        return module

    yield connect
    for module in opened:
        if module._pool is not None:
            module._pool.close()
            module._pool = None
    _drop()


def _store(db, text: str) -> list[float]:
    import embed

    vector = [0.0] * embed.target_dim()
    vector[0] = 1.0
    db.upsert_chunks(
        KEY,
        "brain-arc",
        "arc",
        [
            {
                "chunk_idx": 0,
                "text_preview": text,
                "metadata": {"projects": [PROJECT]},
                "embedding": vector,
            }
        ],
    )
    return vector


def _rows(brain: str) -> list[tuple]:
    with psycopg.connect(_dsn(_principal(brain), _principal(brain))) as conn:
        return conn.execute("SELECT key, text_preview FROM content_embeddings").fetchall()


def test_two_brains_with_the_same_arc_and_project_read_only_their_own(server):
    personal = server("personal")
    query = _store(personal, "personal knowledge")
    swarm = server("swarm")
    _store(swarm, "swarm knowledge")

    found = swarm.search(query, project_id=PROJECT)

    assert [hit["text_preview"] for hit in found] == ["swarm knowledge"]
    assert _rows("personal") == [(KEY, "personal knowledge")]
    with psycopg.connect(_dsn(_principal("personal"), _principal("personal"))) as conn:
        assert conn.execute("SELECT brain_id FROM brain_instance").fetchall() == [("personal",)]


def test_a_principal_cannot_read_another_brains_schema(server):
    server("personal").get_pool()

    with psycopg.connect(_dsn(_principal("swarm"), _principal("swarm"))) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(
                sql.SQL("SELECT * FROM {}.content_embeddings").format(
                    sql.Identifier(_principal("personal"))
                )
            )


def test_a_brain_bound_to_another_brains_schema_refuses_to_start(server):
    personal = server("personal")
    _store(personal, "personal knowledge")

    misbound = server("swarm", principal=_principal("personal"), schema=_principal("personal"))
    with pytest.raises(RuntimeError, match="belongs to brain personal"):
        misbound.get_pool()

    assert misbound._pool is None
    assert _rows("personal") == [(KEY, "personal knowledge")]


def test_a_principal_that_can_read_another_brains_claim_is_refused(server):
    server("personal").get_pool()
    owner, reader = sql.Identifier(_principal("personal")), sql.Identifier(_principal("swarm"))
    _admin(
        sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(owner, reader),
        sql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA {} TO {}").format(owner, reader),
    )

    unrestricted = server("swarm")
    with pytest.raises(RuntimeError, match="can read brain personal"):
        unrestricted.get_pool()

    assert unrestricted._pool is None
