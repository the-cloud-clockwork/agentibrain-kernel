"""Canonical graph entities and typed relations, on two independent fixture sets."""

from __future__ import annotations

import copy
import json
import random
from pathlib import Path

import pytest
from app import graph

FIXTURE = Path(__file__).parent / "fixtures" / "sv2_grf01_graph.json"
MIGRATION = Path(__file__).parents[3] / "migrations" / "knowledge_graph" / "001_canonical_graph.sql"
SETS = ("alpha", "beta")


def _set(name: str) -> dict:
    return copy.deepcopy(json.loads(FIXTURE.read_text())["sets"][name])


def _scope(data: dict) -> graph.Scope:
    return graph.Scope(brain=data["scope"]["brain"], projects=frozenset(data["scope"]["projects"]))


def _built(data: dict) -> graph.KnowledgeGraph:
    return graph.rebuild({"entities": data["entities"], "relations": data["valid"]})


def _arc(data: dict) -> str:
    return data["valid"][0]["source"]


def _views(g: graph.KnowledgeGraph, entity: str, scope: graph.Scope) -> set[tuple[str, str]]:
    return {(v["relation"], v["neighbor"]) for v in g.relations_of(entity, scope)}


# --- A: an arc links to its sessions, lessons and decisions -------------------


@pytest.mark.parametrize("name", SETS)
def test_an_arc_links_to_sessions_lessons_and_decisions(name):
    data = _set(name)
    g = _built(data)
    arc = _arc(data)
    views = g.relations_of(arc, _scope(data))
    by_relation: dict[str, list[dict]] = {}
    for view in views:
        by_relation.setdefault(view["relation"], []).append(view)

    assert {v["neighbor_kind"] for v in by_relation["derived_from"]} == {"session"}
    assert len(by_relation["derived_from"]) == 2
    assert [v["neighbor_kind"] for v in by_relation["has_lesson"]] == ["lesson"]
    assert [v["neighbor_kind"] for v in by_relation["applies_decision"]] == ["decision"]
    lesson_view = by_relation["has_lesson"][0]
    lesson = next(e for e in data["entities"] if e["id"] == lesson_view["neighbor"])
    assert lesson_view["vault_ref"] == lesson["vault_ref"]
    assert lesson_view["status"] == "verified"
    assert lesson_view["confidence"] == "high"
    assert lesson_view["direction"] == "out"
    assert g.invalid_relations_total == {}


@pytest.mark.parametrize("name", SETS)
def test_each_end_reads_the_relation_under_its_own_name(name):
    data = _set(name)
    g = _built(data)
    arc = _arc(data)
    scope = _scope(data)
    lesson = next(r["target"] for r in data["valid"] if r["relation"] == "has_lesson")
    session = next(r["target"] for r in data["valid"] if r["relation"] == "derived_from")

    assert ("lesson_of", arc) in _views(g, lesson, scope)
    assert ("source_of", arc) in _views(g, session, scope)
    incoming = next(v for v in g.relations_of(lesson, scope) if v["relation"] == "lesson_of")
    assert incoming["direction"] == "in"
    assert incoming["stored"] == {"relation": "has_lesson", "source": arc, "target": lesson}


@pytest.mark.parametrize("name", SETS)
def test_an_inverse_pair_is_stored_once(name):
    data = _set(name)
    g = _built(data)
    before = g.snapshot()

    outcome = g.add_relation(graph.Relation.from_dict(data["inverse"]))

    assert outcome.state == "duplicate"
    assert g.snapshot() == before
    assert g.invalid_relations_total == {}


@pytest.mark.parametrize("name", SETS)
def test_neighbours_outside_the_caller_scope_are_not_returned(name):
    data = _set(name)
    g = _built(data)
    arc = _arc(data)
    related = next(r["target"] for r in data["valid"] if r["relation"] == "related")
    other = next(e["project_id"] for e in data["entities"] if e["id"] == related)
    scope = _scope(data)

    assert ("related", related) not in _views(g, arc, scope)
    wider = graph.Scope(brain=scope.brain, projects=scope.projects | {other})
    assert ("related", related) in _views(g, arc, wider)
    elsewhere = graph.Scope(brain="another-brain", projects=wider.projects)
    assert g.relations_of(arc, elsewhere) == []


def test_entity_ids_match_the_knowledge_index_keys():
    assert graph.entity_id("lesson", "mk-1") == "lesson:mk-1"
    assert graph.entity_id("decision", "ADR-0012") == "decision:ADR-0012"
    assert graph.entity_id("arc", "alpha-retry-storm") == "arc:alpha-retry-storm"
    assert graph.ENTITY_KINDS == ("arc", "lesson", "decision", "session", "evidence")
    with pytest.raises(ValueError, match="unknown entity kind: note"):
        graph.entity_id("note", "x")
    with pytest.raises(ValueError, match="entity local id must be non-empty without whitespace"):
        graph.entity_id("arc", "two words")
    with pytest.raises(ValueError, match="entity local id must be non-empty without whitespace"):
        graph.entity_id("arc", "")


def test_an_entity_id_must_carry_its_kind():
    with pytest.raises(ValueError, match="entity id arc:x does not carry kind lesson"):
        graph.Entity.from_dict(
            {
                "id": "arc:x",
                "kind": "lesson",
                "revision": 1,
                "brain": "b",
                "project_id": "p",
                "vault_ref": "r",
            }
        )


def test_every_inverse_name_maps_back_to_its_relation():
    for name, spec in graph.RELATIONS.items():
        assert graph.canonical_name(spec.inverse) == (name, not spec.symmetric)
        assert graph.canonical_name(name) == (name, False)
    assert graph.canonical_name("nonsense") is None


@pytest.mark.parametrize("name", SETS)
def test_a_second_independent_set_does_not_leak_into_the_first(name):
    data = _set(name)
    other = _set("beta" if name == "alpha" else "alpha")
    g = _built(data)
    _built(other)
    assert g.snapshot() == _built(_set(name)).snapshot()


# --- B: rejection and containment -------------------------------------------


@pytest.mark.parametrize(
    ("name", "case", "reason"),
    [
        ("alpha", "missing_target", "unknown_target"),
        ("beta", "missing_target", "unknown_target"),
        ("alpha", "incompatible", "incompatible_types"),
        ("beta", "incompatible", "incompatible_types"),
    ],
)
def test_a_bad_relation_is_quarantined_and_changes_nothing(name, case, reason):
    data = _set(name)
    g = _built(data)
    before = g.snapshot()

    outcome = g.add_relation(graph.Relation.from_dict(data[case]))

    assert outcome.state == "quarantined"
    assert outcome.reason == reason
    assert g.snapshot() == before
    assert g.invalid_relations_total == {reason: 1}
    assert g.quarantine == [{"relation": data[case], "reason": reason}]


@pytest.mark.parametrize("name", SETS)
def test_a_corrected_relation_needs_a_new_request(name):
    data = _set(name)
    g = _built(data)
    bad = data["missing_target"]
    g.add_relation(graph.Relation.from_dict(bad))
    kind = bad["target"].split(":", 1)[0]
    arc = next(e for e in data["entities"] if e["id"] == bad["source"])
    g.add_entity(
        graph.Entity.from_dict({**arc, "id": bad["target"], "kind": kind, "vault_ref": "late"})
    )

    assert all(v["neighbor"] != bad["target"] for v in g.relations_of(bad["source"], _scope(data)))
    assert g.add_relation(graph.Relation.from_dict(bad)).state == "accepted"
    assert len(g.quarantine) == 1


def _alpha_graph() -> tuple[dict, graph.KnowledgeGraph]:
    data = _set("alpha")
    return data, _built(data)


@pytest.mark.parametrize(
    ("relation", "reason"),
    [
        (
            {
                "relation": "mentions",
                "source": "arc:alpha-retry-storm",
                "target": "arc:alpha-queue-drain",
            },
            "unknown_relation",
        ),
        (
            {"relation": "parent", "source": "arc:ghost", "target": "arc:alpha-queue-drain"},
            "unknown_source",
        ),
        (
            {
                "relation": "sibling",
                "source": "arc:alpha-queue-drain",
                "target": "arc:alpha-queue-drain",
            },
            "self_link",
        ),
        (
            {
                "relation": "supersedes",
                "source": "lesson:mk-alpha-backoff",
                "target": "lesson:mk-alpha-backoff",
            },
            "self_link",
        ),
        (
            {
                "relation": "unblocks",
                "source": "arc:alpha-queue-drain",
                "target": "arc:other-project-arc",
            },
            "missing_evidence",
        ),
        (
            {
                "relation": "unblocks",
                "source": "arc:alpha-queue-drain",
                "target": "arc:other-project-arc",
                "evidence": ["evidence:nope"],
            },
            "unknown_evidence",
        ),
        (
            {
                "relation": "unblocks",
                "source": "arc:alpha-queue-drain",
                "target": "arc:other-project-arc",
                "evidence": ["lesson:mk-alpha-backoff"],
            },
            "unknown_evidence",
        ),
        (
            {
                "relation": "unblocks",
                "source": "arc:alpha-queue-drain",
                "target": "arc:alpha-retry-storm",
                "evidence": ["evidence:run-alpha-40100"],
            },
            "contradiction",
        ),
        (
            {
                "relation": "blocked_by",
                "source": "arc:alpha-retry-storm",
                "target": "arc:alpha-queue-drain",
                "evidence": ["evidence:run-alpha-40100"],
            },
            "contradiction",
        ),
        (
            {
                "relation": "blocked_by",
                "source": "arc:alpha-queue-drain",
                "target": "arc:alpha-retry-storm",
                "evidence": ["evidence:run-alpha-40100"],
            },
            "duplicate",
        ),
        (
            {
                "relation": "supersedes",
                "source": "decision:mk-alpha-jitter",
                "target": "lesson:mk-alpha-backoff",
            },
            "incompatible_types",
        ),
        (
            {
                "relation": "parent",
                "source": "arc:alpha-retry-storm",
                "target": "arc:alpha-queue-drain",
                "status": "certain",
            },
            "invalid_status",
        ),
        (
            {
                "relation": "parent",
                "source": "arc:alpha-retry-storm",
                "target": "arc:alpha-queue-drain",
                "confidence": "sure",
            },
            "invalid_confidence",
        ),
        (
            {
                "relation": "parent",
                "source": "arc:alpha-retry-storm",
                "target": "arc:alpha-queue-drain",
                "revision": 0,
            },
            "invalid_revision",
        ),
    ],
)
def test_schema_rules_reject_each_bad_relation(relation, reason):
    data, g = _alpha_graph()
    before = g.snapshot()
    payload = {"status": "suggested", "confidence": "low", "revision": 1, **relation}

    outcome = g.add_relation(graph.Relation.from_dict(payload))

    if reason == "duplicate":
        assert outcome.state == "duplicate"
        assert g.invalid_relations_total == {}
    else:
        assert (outcome.state, outcome.reason) == ("quarantined", reason)
        assert g.invalid_relations_total == {reason: 1}
    assert g.snapshot() == before


def test_reverse_parent_assertions_are_a_contradiction_not_a_priority_pick():
    data, g = _alpha_graph()
    a, b = "arc:alpha-retry-storm", "arc:alpha-queue-drain"
    base = {"status": "suggested", "confidence": "low", "revision": 1}
    assert (
        g.add_relation(
            graph.Relation.from_dict({**base, "relation": "parent", "source": a, "target": b})
        ).state
        == "accepted"
    )

    outcome = g.add_relation(
        graph.Relation.from_dict({**base, "relation": "child", "source": a, "target": b})
    )

    assert (outcome.state, outcome.reason) == ("quarantined", "contradiction")
    assert ("parent", b) in _views(g, a, graph.Scope("swarm", frozenset({"github.com/acme/alpha"})))


def test_several_relation_types_may_join_one_pair():
    data, g = _alpha_graph()
    a, b = "arc:alpha-retry-storm", "arc:alpha-queue-drain"
    base = {"status": "suggested", "confidence": "low", "revision": 1}
    assert (
        g.add_relation(
            graph.Relation.from_dict({**base, "relation": "related", "source": b, "target": a})
        ).state
        == "accepted"
    )
    views = _views(g, a, graph.Scope("swarm", frozenset({"github.com/acme/alpha"})))
    assert {("unblocks", b), ("related", b)} <= views


def test_a_relation_across_brains_is_quarantined():
    data, g = _alpha_graph()
    beta = _set("beta")
    for entity in beta["entities"]:
        g.add_entity(graph.Entity.from_dict(entity))
    before = g.snapshot()
    payload = {
        "relation": "related",
        "source": "arc:alpha-retry-storm",
        "target": "arc:beta-cache-miss",
        "status": "suggested",
        "confidence": "low",
        "revision": 1,
    }

    outcome = g.add_relation(graph.Relation.from_dict(payload))

    assert (outcome.state, outcome.reason) == ("quarantined", "scope_mismatch")
    assert g.snapshot() == before


def test_a_quarantine_record_holds_only_the_submitted_relation():
    data, g = _alpha_graph()
    g.add_relation(graph.Relation.from_dict(data["missing_target"]))
    record = g.quarantine[0]
    assert set(record) == {"relation", "reason"}
    assert record["relation"] == data["missing_target"]


def test_an_entity_of_unknown_kind_is_refused():
    with pytest.raises(ValueError, match="unknown entity kind: note"):
        graph.Entity.from_dict(
            {
                "id": "note:x",
                "kind": "note",
                "revision": 1,
                "brain": "b",
                "project_id": "p",
                "vault_ref": "r",
            }
        )


# --- C: recovery and replay ------------------------------------------------


@pytest.mark.parametrize("name", SETS)
def test_rebuild_after_an_interrupted_build_matches_the_full_build(name):
    data = _set(name)
    relations = data["valid"] + [data["inverse"], data["missing_target"], data["incompatible"]]
    metadata = {"entities": data["entities"], "relations": relations}
    full = graph.rebuild(metadata)

    partial = graph.KnowledgeGraph()
    for entity in data["entities"]:
        partial.add_entity(graph.Entity.from_dict(entity))
    for relation in relations[: len(relations) // 2]:
        partial.add_relation(graph.Relation.from_dict(relation))
    restarted = graph.rebuild(metadata)

    assert restarted.snapshot() == full.snapshot()
    assert restarted.quarantine == full.quarantine
    assert (
        restarted.invalid_relations_total
        == full.invalid_relations_total
        == {
            "unknown_target": 1,
            "incompatible_types": 1,
        }
    )
    for relation in relations:
        partial.add_relation(graph.Relation.from_dict(relation))
    assert partial.snapshot() == full.snapshot()


@pytest.mark.parametrize("name", SETS)
def test_rebuild_does_not_depend_on_input_order(name):
    data = _set(name)
    contradicting = {
        "relation": "blocked_by",
        "source": data["valid"][5]["source"],
        "target": data["valid"][5]["target"],
        "status": "suggested",
        "confidence": "low",
        "evidence": data["valid"][5]["evidence"],
        "revision": 1,
    }
    relations = data["valid"] + [data["inverse"], contradicting]
    expected = graph.rebuild({"entities": data["entities"], "relations": relations})
    for seed in (1, 2, 3):
        shuffled = list(relations)
        entities = list(data["entities"])
        random.Random(seed).shuffle(shuffled)
        random.Random(seed).shuffle(entities)
        got = graph.rebuild({"entities": entities, "relations": shuffled})
        assert got.snapshot() == expected.snapshot()
        assert got.quarantine == expected.quarantine


@pytest.mark.parametrize("name", SETS)
def test_a_replayed_relation_adds_nothing(name):
    data = _set(name)
    g = _built(data)
    before = g.snapshot()
    for relation in data["valid"]:
        assert g.add_relation(graph.Relation.from_dict(relation)).state == "duplicate"
    assert g.snapshot() == before


@pytest.mark.parametrize("name", SETS)
def test_an_older_revision_never_overwrites_a_newer_one(name):
    data = _set(name)
    g = _built(data)
    newest = max(data["entities"], key=lambda e: e["revision"])
    stale = {**newest, "revision": newest["revision"] - 1, "vault_ref": "stale"}
    assert g.add_entity(graph.Entity.from_dict(stale)).state == "stale"

    relation = next(r for r in data["valid"] if r["relation"] == "has_lesson")
    newer = {**relation, "revision": relation["revision"] + 1, "status": "approved"}
    assert g.add_relation(graph.Relation.from_dict(newer)).state == "accepted"
    older = {**relation, "status": "suggested"}
    assert g.add_relation(graph.Relation.from_dict(older)).state == "stale"

    snapshot = g.snapshot()
    kept = next(e for e in snapshot["entities"] if e["id"] == newest["id"])
    assert kept["vault_ref"] == newest["vault_ref"]
    edge = next(r for r in snapshot["relations"] if r["relation"] == "has_lesson")
    assert (edge["status"], edge["revision"]) == ("approved", relation["revision"] + 1)


def test_same_revision_with_different_content_is_a_conflict():
    data, g = _alpha_graph()
    relation = {**data["valid"][2], "status": "suggested"}
    outcome = g.add_relation(graph.Relation.from_dict(relation))
    assert (outcome.state, outcome.reason) == ("quarantined", "revision_conflict")


# --- migration seed matches the schema --------------------------------------


def test_the_migration_seeds_exactly_the_code_schema():
    text = MIGRATION.read_text()
    start = text.index("-- BEGIN SEED\n") + len("-- BEGIN SEED\n")
    end = text.index("-- END SEED")
    assert text[start:end] == graph.seed_sql()
