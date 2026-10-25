"""Canonical knowledge graph: addressable entities and typed, directed relations.

Arcs, lessons, decisions, source sessions and evidence are entities with stable
ids of the form `<kind>:<local id>`, matching the knowledge index keys. A
relation is stored once, in its canonical direction; its inverse name is a
read-side view. A relation that names an unknown endpoint, joins incompatible
kinds or contradicts an accepted one is quarantined and counted, never applied.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

ENTITY_KINDS = ("arc", "lesson", "decision", "session", "evidence")
STATUSES = ("suggested", "verified", "approved")
CONFIDENCES = ("low", "medium", "high")


@dataclass(frozen=True)
class RelationType:
    inverse: str
    pairs: frozenset[tuple[str, str]]
    symmetric: bool = False
    allow_self: bool = False
    requires_evidence: bool = False


def _pairs(*pairs: tuple[str, str]) -> frozenset[tuple[str, str]]:
    return frozenset(pairs)


_SAME_KIND = _pairs(("arc", "arc"), ("lesson", "lesson"), ("decision", "decision"))

RELATIONS: dict[str, RelationType] = {
    "derived_from": RelationType("source_of", _pairs(("arc", "session"))),
    "has_lesson": RelationType("lesson_of", _pairs(("arc", "lesson"))),
    "applies_decision": RelationType("decision_applies_to", _pairs(("arc", "decision"))),
    "supported_by": RelationType(
        "supports", _pairs(("arc", "evidence"), ("lesson", "evidence"), ("decision", "evidence"))
    ),
    "parent": RelationType("child", _pairs(("arc", "arc"))),
    "supersedes": RelationType("superseded_by", _SAME_KIND),
    "unblocks": RelationType("blocked_by", _pairs(("arc", "arc")), requires_evidence=True),
    "sibling": RelationType("sibling", _pairs(("arc", "arc")), symmetric=True),
    "related": RelationType("related", _pairs(("arc", "arc")), symmetric=True),
}

_INVERSES = {spec.inverse: name for name, spec in RELATIONS.items() if not spec.symmetric}


def canonical_name(name: str) -> tuple[str, bool] | None:
    """Return the stored relation for a name and whether its endpoints swap."""
    if name in RELATIONS:
        return name, False
    if name in _INVERSES:
        return _INVERSES[name], True
    return None


def entity_id(kind: str, local: str) -> str:
    if kind not in ENTITY_KINDS:
        raise ValueError(f"unknown entity kind: {kind}")
    if not local or any(ch.isspace() for ch in local):
        raise ValueError("entity local id must be non-empty without whitespace")
    return f"{kind}:{local}"


@dataclass(frozen=True)
class Scope:
    brain: str
    projects: frozenset[str]

    def allows(self, entity: Entity) -> bool:
        return entity.brain == self.brain and entity.project_id in self.projects


@dataclass(frozen=True)
class Entity:
    id: str
    kind: str
    revision: int
    brain: str
    project_id: str
    vault_ref: str

    @classmethod
    def from_dict(cls, data: dict) -> Entity:
        kind = data["kind"]
        if kind not in ENTITY_KINDS:
            raise ValueError(f"unknown entity kind: {kind}")
        if not str(data["id"]).startswith(f"{kind}:"):
            raise ValueError(f"entity id {data['id']} does not carry kind {kind}")
        return cls(
            id=data["id"],
            kind=kind,
            revision=int(data["revision"]),
            brain=data["brain"],
            project_id=data["project_id"],
            vault_ref=data["vault_ref"],
        )

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "revision": self.revision,
            "brain": self.brain,
            "project_id": self.project_id,
            "vault_ref": self.vault_ref,
        }


@dataclass(frozen=True)
class Relation:
    relation: str
    source: str
    target: str
    status: str
    confidence: str
    revision: int
    evidence: tuple[str, ...] = ()
    raw: dict = field(default_factory=dict, compare=False, hash=False)

    @classmethod
    def from_dict(cls, data: dict) -> Relation:
        return cls(
            relation=data["relation"],
            source=data["source"],
            target=data["target"],
            status=data["status"],
            confidence=data["confidence"],
            revision=int(data["revision"]),
            evidence=tuple(sorted(data.get("evidence", ()))),
            raw=dict(data),
        )

    @property
    def key(self) -> tuple[str, str, str]:
        return self.relation, self.source, self.target

    def as_dict(self) -> dict:
        return {
            "relation": self.relation,
            "source": self.source,
            "target": self.target,
            "status": self.status,
            "confidence": self.confidence,
            "evidence": list(self.evidence),
            "revision": self.revision,
        }


@dataclass(frozen=True)
class Outcome:
    state: str
    reason: str = ""


def canonical(relation: Relation) -> Relation | None:
    """The stored form: canonical name, swapped endpoints for an inverse, sorted for symmetry."""
    found = canonical_name(relation.relation)
    if found is None:
        return None
    name, swap = found
    source, target = relation.source, relation.target
    if swap or (RELATIONS[name].symmetric and source > target):
        source, target = target, source
    return Relation(
        name,
        source,
        target,
        relation.status,
        relation.confidence,
        relation.revision,
        relation.evidence,
        relation.raw,
    )


class KnowledgeGraph:
    def __init__(self) -> None:
        self.entities: dict[str, Entity] = {}
        self.relations: dict[tuple[str, str, str], Relation] = {}
        self.quarantine: list[dict] = []
        self.invalid_relations_total: dict[str, int] = Counter()

    def add_entity(self, entity: Entity) -> Outcome:
        current = self.entities.get(entity.id)
        if current is not None and current.revision > entity.revision:
            return Outcome("stale")
        if current == entity:
            return Outcome("duplicate")
        self.entities[entity.id] = entity
        return Outcome("accepted")

    def add_relation(self, relation: Relation) -> Outcome:
        stored = canonical(relation)
        reason = "unknown_relation" if stored is None else self._invalid(stored)
        if reason:
            return self._quarantine(relation, reason)
        current = self.relations.get(stored.key)
        if current is not None:
            if current.revision > stored.revision:
                return Outcome("stale")
            if current.revision == stored.revision:
                if current == stored:
                    return Outcome("duplicate")
                return self._quarantine(relation, "revision_conflict")
        self.relations[stored.key] = stored
        return Outcome("accepted")

    def _invalid(self, rel: Relation) -> str:
        if rel.status not in STATUSES:
            return "invalid_status"
        if rel.confidence not in CONFIDENCES:
            return "invalid_confidence"
        if rel.revision < 1:
            return "invalid_revision"
        source = self.entities.get(rel.source)
        target = self.entities.get(rel.target)
        if source is None:
            return "unknown_source"
        if target is None:
            return "unknown_target"
        spec = RELATIONS[rel.relation]
        if (source.kind, target.kind) not in spec.pairs:
            return "incompatible_types"
        if rel.source == rel.target and not spec.allow_self:
            return "self_link"
        if source.brain != target.brain:
            return "scope_mismatch"
        if spec.requires_evidence and not rel.evidence:
            return "missing_evidence"
        for ref in rel.evidence:
            found = self.entities.get(ref)
            if found is None or found.kind != "evidence" or found.brain != source.brain:
                return "unknown_evidence"
        if not spec.symmetric and (rel.relation, rel.target, rel.source) in self.relations:
            return "contradiction"
        return ""

    def _quarantine(self, relation: Relation, reason: str) -> Outcome:
        self.quarantine.append({"relation": relation.raw, "reason": reason})
        self.invalid_relations_total[reason] += 1
        return Outcome("quarantined", reason)

    def relations_of(self, entity: str, scope: Scope) -> list[dict]:
        """Typed relations of one entity, each read under its own name, inside the caller scope."""
        own = self.entities.get(entity)
        if own is None or not scope.allows(own):
            return []
        views = []
        for rel in sorted(self.relations.values(), key=lambda r: r.key):
            if entity not in (rel.source, rel.target):
                continue
            outgoing = rel.source == entity
            neighbour = self.entities[rel.target if outgoing else rel.source]
            if not scope.allows(neighbour):
                continue
            spec = RELATIONS[rel.relation]
            views.append(
                {
                    "relation": rel.relation if outgoing or spec.symmetric else spec.inverse,
                    "direction": "out" if outgoing else "in",
                    "neighbor": neighbour.id,
                    "neighbor_kind": neighbour.kind,
                    "vault_ref": neighbour.vault_ref,
                    "status": rel.status,
                    "confidence": rel.confidence,
                    "evidence": list(rel.evidence),
                    "stored": {
                        "relation": rel.relation,
                        "source": rel.source,
                        "target": rel.target,
                    },
                }
            )
        return views

    def snapshot(self) -> dict:
        return {
            "entities": [self.entities[k].as_dict() for k in sorted(self.entities)],
            "relations": [self.relations[k].as_dict() for k in sorted(self.relations)],
        }


def _replay_order(relation: Relation) -> tuple:
    stored = canonical(relation) or relation
    return (relation.revision, stored.key, relation.relation, relation.status, relation.confidence)


def rebuild(metadata: dict) -> KnowledgeGraph:
    """Project canonical metadata into a graph; the result does not depend on input order."""
    graph = KnowledgeGraph()
    entities = sorted(
        (Entity.from_dict(e) for e in metadata.get("entities", ())),
        key=lambda e: (e.id, e.revision),
    )
    for entity in entities:
        graph.add_entity(entity)
    relations = sorted(
        (Relation.from_dict(r) for r in metadata.get("relations", ())), key=_replay_order
    )
    for relation in relations:
        graph.add_relation(relation)
    return graph


def _sql_text(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def seed_sql() -> str:
    """The schema rows the knowledge graph migration seeds, generated from RELATIONS."""
    lines = []
    for kind in ENTITY_KINDS:
        lines.append(
            f"INSERT INTO graph_entity_kinds (kind) VALUES ({_sql_text(kind)}) ON CONFLICT DO NOTHING;"
        )
    for name, spec in RELATIONS.items():
        flags = ", ".join(
            str(v).lower() for v in (spec.symmetric, spec.allow_self, spec.requires_evidence)
        )
        lines.append(
            "INSERT INTO graph_relation_types"
            " (relation, inverse, is_symmetric, allow_self, requires_evidence)"
            f" VALUES ({_sql_text(name)}, {_sql_text(spec.inverse)}, {flags})"
            " ON CONFLICT DO NOTHING;"
        )
    for name, spec in RELATIONS.items():
        for source_kind, target_kind in sorted(spec.pairs):
            lines.append(
                "INSERT INTO graph_relation_pairs (relation, source_kind, target_kind)"
                f" VALUES ({_sql_text(name)}, {_sql_text(source_kind)}, {_sql_text(target_kind)})"
                " ON CONFLICT DO NOTHING;"
            )
    return "\n".join(lines) + "\n"
