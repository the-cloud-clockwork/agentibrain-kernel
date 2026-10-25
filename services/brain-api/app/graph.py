"""Knowledge graph: typed entities and directed relations over a relational edge table.

Entities are arcs, source sessions, lessons, decisions and evidence, each with a
stable ``<type>:<key>`` id, a revision, its project scope, its provenance and the
vault reference that stays the human readable copy. Relations are directed and
typed; an inverse name is stored as its canonical edge. The tables are a
projection: ``rebuild`` restores them from canonical metadata. The graph is
inactive unless BRAIN_GRAPH_DB names its SQLite file.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).parent / "migrations" / "knowledge_graph"
ENTITY_TYPES = frozenset({"arc", "session", "lesson", "decision", "evidence"})
STATUSES = frozenset({"proposed", "accepted", "retired"})
CONFIDENCE_CLASSES = frozenset({"observed", "extracted", "inferred"})
_KEY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:#/-]{0,199}")


@dataclass(frozen=True)
class RelationType:
    name: str
    sources: frozenset[str]
    targets: frozenset[str]
    inverse: str


RELATION_TYPES = {
    t.name: t
    for t in (
        RelationType("derived_from", frozenset({"arc"}), frozenset({"session"}), "source_of"),
        RelationType("has_lesson", frozenset({"arc"}), frozenset({"lesson"}), "lesson_of"),
        RelationType(
            "applies_decision", frozenset({"arc"}), frozenset({"decision"}), "decision_applied_by"
        ),
        RelationType(
            "supported_by",
            frozenset({"arc", "lesson", "decision"}),
            frozenset({"evidence"}),
            "supports",
        ),
    )
}
INVERSES = {t.inverse: t.name for t in RELATION_TYPES.values()}

Visible = Callable[[dict], bool]


class GraphRejection(ValueError):
    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason


def entity_id(entity_type: str, key: str) -> str:
    if not isinstance(entity_type, str) or entity_type not in ENTITY_TYPES:
        raise GraphRejection("unknown_entity_type", f"entity type {entity_type!r} is not known")
    if not isinstance(key, str) or not _KEY_RE.fullmatch(key):
        raise GraphRejection("invalid_entity", f"entity key {key!r} is not a valid key")
    return f"{entity_type}:{key}"


def _entity(record: dict) -> dict:
    if not isinstance(record, dict):
        raise GraphRejection("invalid_entity", "an entity must be an object")
    kind = record.get("type")
    ident = record.get("id")
    if not isinstance(ident, str) or entity_id(kind, ident.partition(":")[2]) != ident:
        raise GraphRejection("invalid_entity", f"entity id {ident!r} does not name type {kind!r}")
    revision = record.get("revision")
    projects = record.get("projects", [])
    vault_ref = record.get("vault_ref")
    title = record.get("title", "")
    provenance = record.get("provenance")
    if type(revision) is not int or revision < 1:
        raise GraphRejection("invalid_entity", "revision must be a positive integer")
    if not isinstance(projects, list) or not all(
        isinstance(p, str) and p.strip() for p in projects
    ):
        raise GraphRejection("invalid_entity", "projects must be a list of project ids")
    if vault_ref is not None and not isinstance(vault_ref, str):
        raise GraphRejection("invalid_entity", "vault_ref must be a string or null")
    if not isinstance(title, str):
        raise GraphRejection("invalid_entity", "title must be a string")
    if not (
        isinstance(provenance, dict)
        and isinstance(provenance.get("source"), str)
        and provenance["source"].strip()
        and isinstance(provenance.get("revision"), str)
    ):
        raise GraphRejection("invalid_entity", "provenance needs a source and its revision")
    return {
        "id": ident,
        "type": kind,
        "revision": revision,
        "projects": sorted({p.strip() for p in projects}),
        "vault_ref": vault_ref,
        "title": title,
        "provenance": {"source": provenance["source"], "revision": provenance["revision"]},
    }


def _relation_fields(record: dict) -> dict:
    if not isinstance(record, dict):
        raise GraphRejection("invalid_relation", "a relation must be an object")
    name = record.get("type")
    source, target = record.get("source"), record.get("target")
    if not isinstance(name, str):
        raise GraphRejection("unknown_relation_type", f"relation type {name!r} is not known")
    if name in INVERSES:
        name, source, target = INVERSES[name], target, source
    elif name not in RELATION_TYPES:
        raise GraphRejection("unknown_relation_type", f"relation type {name!r} is not known")
    status = record.get("status", "proposed")
    confidence = record.get("confidence")
    evidence = record.get("evidence", [])
    revision = record.get("revision", 1)
    if type(revision) is not int or revision < 1:
        raise GraphRejection("invalid_relation", "revision must be a positive integer")
    if not isinstance(status, str) or status not in STATUSES:
        raise GraphRejection("invalid_relation", f"status {status!r} is not known")
    if not isinstance(confidence, str) or confidence not in CONFIDENCE_CLASSES:
        raise GraphRejection("invalid_relation", f"confidence class {confidence!r} is not known")
    if not isinstance(evidence, list) or not all(isinstance(e, str) and e for e in evidence):
        raise GraphRejection("invalid_relation", "evidence must be a list of references")
    if not (isinstance(source, str) and isinstance(target, str)):
        raise GraphRejection("unknown_endpoint", "a relation needs a source and a target id")
    return {
        "source": source,
        "type": name,
        "target": target,
        "revision": revision,
        "status": status,
        "confidence": confidence,
        "evidence": list(evidence),
    }


def relation_id(source: str, name: str, target: str) -> str:
    digest = hashlib.sha256(f"{source}\0{name}\0{target}".encode()).hexdigest()
    return f"rel-{digest[:32]}"


def _entity_row(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "type": row["type"],
        "revision": row["revision"],
        "projects": json.loads(row["projects"]),
        "vault_ref": row["vault_ref"],
        "title": row["title"],
        "provenance": json.loads(row["provenance"]),
    }


def _always(_: dict) -> bool:
    return True


class GraphStore:
    def __init__(self, path: Path | str) -> None:
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._lock = threading.Lock()
        self._migrate()

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        self._conn.execute("COMMIT")

    def _migrate(self) -> None:
        with self._lock:
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS graph_migrations (name TEXT PRIMARY KEY)"
            )
            done = {r["name"] for r in self._conn.execute("SELECT name FROM graph_migrations")}
            for sql in sorted(MIGRATIONS_DIR.glob("*.sql")):
                if sql.name in done:
                    continue
                self._conn.execute("BEGIN IMMEDIATE")
                try:
                    for statement in sql.read_text(encoding="utf-8").split(";"):
                        if statement.strip():
                            self._conn.execute(statement)
                    self._conn.execute(
                        "INSERT INTO graph_migrations (name) VALUES (?)", (sql.name,)
                    )
                except BaseException:
                    self._conn.execute("ROLLBACK")
                    raise
                self._conn.execute("COMMIT")

    def _read_entity(self, ident: str) -> dict | None:
        row = self._conn.execute("SELECT * FROM graph_entities WHERE id = ?", (ident,)).fetchone()
        return None if row is None else _entity_row(row)

    def _write_entity(self, entity: dict) -> None:
        self._conn.execute(
            "INSERT INTO graph_entities"
            " (id, type, revision, projects, vault_ref, title, provenance)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (id) DO UPDATE SET type = excluded.type, revision = excluded.revision,"
            " projects = excluded.projects, vault_ref = excluded.vault_ref,"
            " title = excluded.title, provenance = excluded.provenance",
            (
                entity["id"],
                entity["type"],
                entity["revision"],
                json.dumps(entity["projects"]),
                entity["vault_ref"],
                entity["title"],
                json.dumps(entity["provenance"], sort_keys=True),
            ),
        )

    def get_entity(self, ident: str) -> dict | None:
        with self._lock:
            return self._read_entity(ident)

    def put_entity(self, record: dict) -> str:
        """Return created, updated or replayed; stale when the stored revision is newer."""
        entity = _entity(record)
        with self._lock, self._transaction():
            stored = self._read_entity(entity["id"])
            if stored == entity:
                return "replayed"
            if stored is not None and stored["revision"] >= entity["revision"]:
                return "stale"
            self._write_entity(entity)
            return "created" if stored is None else "updated"

    def _check_relation(self, record: dict, visible: Visible) -> dict:
        relation = _relation_fields(record)
        schema = RELATION_TYPES[relation["type"]]
        ends = []
        for end in ("source", "target"):
            entity = self._read_entity(relation[end])
            if entity is None or not visible(entity):
                raise GraphRejection("unknown_endpoint", f"{end} {relation[end]!r} is not known")
            ends.append(entity["type"])
        if ends[0] not in schema.sources or ends[1] not in schema.targets:
            raise GraphRejection(
                "incompatible_endpoint",
                f"{schema.name} links {sorted(schema.sources)} to {sorted(schema.targets)},"
                f" not {ends[0]} to {ends[1]}",
            )
        return relation

    def _count_invalid(self, reason: str) -> None:
        self._conn.execute(
            "INSERT INTO graph_invalid_relations (reason, total) VALUES (?, 1)"
            " ON CONFLICT (reason) DO UPDATE SET total = total + 1",
            (reason,),
        )

    def _upsert_relation(self, relation: dict) -> str:
        ident = relation_id(relation["source"], relation["type"], relation["target"])
        row = self._conn.execute(
            "SELECT revision, status, confidence, evidence FROM graph_relations WHERE id = ?",
            (ident,),
        ).fetchone()
        values = (
            relation["revision"],
            relation["status"],
            relation["confidence"],
            json.dumps(relation["evidence"]),
        )
        if row is not None and tuple(row) == values:
            return "replayed"
        if row is not None and row["revision"] >= relation["revision"]:
            return "stale"
        self._conn.execute(
            "INSERT OR REPLACE INTO graph_relations"
            " (id, source, type, target, revision, status, confidence, evidence)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (ident, relation["source"], relation["type"], relation["target"], *values),
        )
        return "created" if row is None else "updated"

    def put_relation(
        self, record: dict, *, quarantine: bool = False, visible: Visible = _always
    ) -> dict:
        """Store a relation, or raise GraphRejection; quarantine=True holds a bad one instead."""
        with self._lock:
            try:
                with self._transaction():
                    relation = self._check_relation(record, visible)
                    action = self._upsert_relation(relation)
            except GraphRejection as exc:
                with self._transaction():
                    self._count_invalid(exc.reason)
                    if quarantine:
                        self._conn.execute(
                            "INSERT INTO graph_quarantine (reason, record) VALUES (?, ?)",
                            (exc.reason, json.dumps(record, sort_keys=True)),
                        )
                if not quarantine:
                    raise
                return {"action": "quarantined", "reason": exc.reason}
        ident = relation_id(relation["source"], relation["type"], relation["target"])
        return {"action": action, "relation_id": ident, "relation": relation}

    def relations(self, ident: str, visible: Visible = _always) -> list[dict]:
        """Every edge touching an entity, named from its side, limited to visible neighbours."""
        columns = (
            "r.id, r.type, r.revision, r.status, r.confidence, r.evidence, e.id, e.type, e.projects"
        )
        with self._lock:
            rows = self._conn.execute(
                f"SELECT 'out', {columns} FROM graph_relations r"
                " JOIN graph_entities e ON e.id = r.target WHERE r.source = ?"
                f" UNION ALL SELECT 'in', {columns} FROM graph_relations r"
                " JOIN graph_entities e ON e.id = r.source WHERE r.target = ?",
                (ident, ident),
            ).fetchall()
        views = []
        for (
            direction,
            rel,
            kind,
            revision,
            status,
            confidence,
            evidence,
            other,
            other_type,
            projects,
        ) in rows:
            if not visible({"id": other, "type": other_type, "projects": json.loads(projects)}):
                continue
            views.append(
                {
                    "relation_id": rel,
                    "type": kind,
                    "name": kind if direction == "out" else RELATION_TYPES[kind].inverse,
                    "direction": direction,
                    "other": other,
                    "revision": revision,
                    "status": status,
                    "confidence": confidence,
                    "evidence": json.loads(evidence),
                }
            )
        return sorted(views, key=lambda v: (v["direction"], v["name"], v["other"]))

    def quarantined(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM graph_quarantine ORDER BY seq").fetchall()
        return [
            {"seq": r["seq"], "reason": r["reason"], "record": json.loads(r["record"])}
            for r in rows
        ]

    def invalid_relations_total(self) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT reason, total FROM graph_invalid_relations"
            ).fetchall()
        return {r["reason"]: r["total"] for r in rows}

    def snapshot(self) -> tuple[frozenset, frozenset]:
        with self._lock:
            entities = self._conn.execute(
                "SELECT id, type, revision, projects, vault_ref, title, provenance"
                " FROM graph_entities"
            ).fetchall()
            relations = self._conn.execute(
                "SELECT source, type, target, revision, status, confidence, evidence"
                " FROM graph_relations"
            ).fetchall()
        return frozenset(tuple(r) for r in entities), frozenset(tuple(r) for r in relations)

    def rebuild(self, canonical: dict) -> dict:
        """Replace the projection with what canonical metadata yields, in one transaction."""
        newest: dict[str, dict] = {}
        for record in canonical.get("entities", []):
            entity = _entity(record)
            kept = newest.get(entity["id"])
            if kept is None or entity["revision"] > kept["revision"]:
                newest[entity["id"]] = entity
        rejected: dict[str, int] = {}
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute("DELETE FROM graph_relations")
                self._conn.execute("DELETE FROM graph_entities")
                for entity in newest.values():
                    self._write_entity(entity)
                for record in canonical.get("relations", []):
                    try:
                        self._upsert_relation(self._check_relation(record, _always))
                    except GraphRejection as exc:
                        rejected[exc.reason] = rejected.get(exc.reason, 0) + 1
                relations = self._conn.execute("SELECT COUNT(*) FROM graph_relations").fetchone()
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")
        return {"entities": len(newest), "relations": relations[0], "rejected": rejected}


_stores: dict[str, GraphStore] = {}
_stores_lock = threading.Lock()


def active_store() -> GraphStore | None:
    path = os.getenv("BRAIN_GRAPH_DB", "").strip()
    if not path:
        return None
    with _stores_lock:
        if path not in _stores:
            _stores[path] = GraphStore(path)
        return _stores[path]
