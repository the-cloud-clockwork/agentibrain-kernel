"""The knowledge graph migration on a real Postgres refuses unknown endpoints and incompatible kinds."""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

POSTGRES_URL = os.environ.get("TICK_CYCLE_POSTGRES_URL", "")
MIGRATIONS = Path(__file__).parents[3] / "migrations" / "knowledge_graph"
UP = MIGRATIONS / "001_canonical_graph.sql"
DOWN = MIGRATIONS / "001_canonical_graph.down.sql"
TABLES = {
    "graph_entity_kinds",
    "graph_relation_types",
    "graph_relation_pairs",
    "graph_entities",
    "graph_relations",
    "graph_relation_quarantine",
}

pytestmark = pytest.mark.skipif(
    not os.environ.get("CI") and not POSTGRES_URL,
    reason="needs TICK_CYCLE_POSTGRES_URL",
)

ENTITIES = [
    ("arc:alpha-retry-storm", "arc"),
    ("session:5f1c2a9e-alpha-1", "session"),
    ("lesson:mk-alpha-backoff", "lesson"),
    ("decision:mk-alpha-jitter", "decision"),
]


@pytest.fixture()
def conn():
    import psycopg

    schema = f"grf01_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(POSTGRES_URL, autocommit=True) as connection:
        connection.execute(f"CREATE SCHEMA {schema}")
        connection.execute(f"SET search_path TO {schema}")
        try:
            yield connection
        finally:
            connection.execute(f"DROP SCHEMA {schema} CASCADE")


def _tables(conn) -> set[str]:
    rows = conn.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema()"
    ).fetchall()
    return {r[0] for r in rows}


def _seed(conn) -> None:
    conn.execute(UP.read_text())
    for entity_id, kind in ENTITIES:
        conn.execute(
            "INSERT INTO graph_entities (id, kind, brain, project_id, revision, vault_ref)"
            " VALUES (%s, %s, 'swarm', 'github.com/acme/alpha', 1, %s)",
            (entity_id, kind, f"clusters/{entity_id}.md"),
        )


def _relate(conn, relation, source, source_kind, target, target_kind) -> None:
    conn.execute(
        "INSERT INTO graph_relations (relation, source_id, source_kind, target_id, target_kind,"
        " status, confidence, revision) VALUES (%s, %s, %s, %s, %s, 'verified', 'high', 1)",
        (relation, source, source_kind, target, target_kind),
    )


def _count(conn) -> int:
    return conn.execute("SELECT count(*) FROM graph_relations").fetchone()[0]


def test_a_valid_arc_lesson_link_is_stored_and_read_from_both_ends(conn):
    _seed(conn)
    _relate(conn, "has_lesson", "arc:alpha-retry-storm", "arc", "lesson:mk-alpha-backoff", "lesson")
    _relate(
        conn, "derived_from", "arc:alpha-retry-storm", "arc", "session:5f1c2a9e-alpha-1", "session"
    )
    _relate(
        conn,
        "applies_decision",
        "arc:alpha-retry-storm",
        "arc",
        "decision:mk-alpha-jitter",
        "decision",
    )

    out = conn.execute(
        "SELECT relation, target_id FROM graph_relations WHERE source_id = 'arc:alpha-retry-storm'"
        " ORDER BY relation"
    ).fetchall()
    assert [r[0] for r in out] == ["applies_decision", "derived_from", "has_lesson"]
    inverse = conn.execute(
        "SELECT t.inverse, r.source_id FROM graph_relations r JOIN graph_relation_types t USING (relation)"
        " WHERE r.target_id = 'lesson:mk-alpha-backoff'"
    ).fetchall()
    assert inverse == [("lesson_of", "arc:alpha-retry-storm")]


def test_a_missing_target_is_refused_and_changes_nothing(conn):
    import psycopg

    _seed(conn)
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        _relate(
            conn, "has_lesson", "arc:alpha-retry-storm", "arc", "lesson:mk-never-written", "lesson"
        )
    assert _count(conn) == 0


def test_an_incompatible_relation_type_is_refused(conn):
    import psycopg

    _seed(conn)
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        _relate(
            conn,
            "has_lesson",
            "arc:alpha-retry-storm",
            "arc",
            "decision:mk-alpha-jitter",
            "decision",
        )
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        _relate(
            conn, "has_lesson", "arc:alpha-retry-storm", "arc", "decision:mk-alpha-jitter", "lesson"
        )
    assert _count(conn) == 0


def test_an_inverse_name_cannot_be_stored_as_a_relation(conn):
    import psycopg

    _seed(conn)
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        _relate(
            conn, "lesson_of", "lesson:mk-alpha-backoff", "lesson", "arc:alpha-retry-storm", "arc"
        )
    assert _count(conn) == 0


def test_an_entity_id_must_carry_its_kind(conn):
    import psycopg

    conn.execute(UP.read_text())
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            "INSERT INTO graph_entities (id, kind, brain, project_id, revision, vault_ref)"
            " VALUES ('arc:x', 'lesson', 'swarm', 'p', 1, 'r')"
        )


def test_down_drops_only_the_graph_tables_and_up_applies_again(conn):
    conn.execute("CREATE TABLE unrelated_source (id int)")
    conn.execute(UP.read_text())
    assert TABLES <= _tables(conn)
    conn.execute(DOWN.read_text())
    assert _tables(conn) == {"unrelated_source"}
    _seed(conn)
    conn.execute(UP.read_text())
    assert TABLES <= _tables(conn)
