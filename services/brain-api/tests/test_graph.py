"""SV2-GRF-01: typed graph entities and relations with inverse semantics, rejection and rebuild."""

from __future__ import annotations

import copy
import importlib
import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "sv2_grf01_graph.json").read_text(encoding="utf-8")
)
SETS = ("alpha", "beta")
ROUTER = "router-bearer"
_MODULES = [
    "app.feed",
    "app.signal",
    "app.markers",
    "app.tick_trigger",
    "app.pipeline",
    "app.scope",
    "app.graph",
    "app.context",
    "app.main",
]


def _graph():
    return importlib.import_module("app.graph")


def _store(tmp_path: Path, name: str = "graph.db"):
    return _graph().GraphStore(tmp_path / name)


def _loaded(tmp_path: Path, fixture: dict, name: str = "graph.db"):
    store = _store(tmp_path, name)
    for entity in fixture["entities"]:
        store.put_entity(entity)
    for relation in fixture["valid"]:
        store.put_relation(relation)
    return store


def _client(monkeypatch, vault: Path, db: Path | None, worker_tokens: str = "") -> TestClient:
    monkeypatch.setenv("KB_ROUTER_TOKENS", ROUTER)
    monkeypatch.setenv("BRAIN_ID", "hive-a")
    monkeypatch.setenv("KB_WORKER_TOKENS", worker_tokens)
    if db is None:
        monkeypatch.delenv("BRAIN_GRAPH_DB", raising=False)
    else:
        monkeypatch.setenv("BRAIN_GRAPH_DB", str(db))
    for name in _MODULES:
        if name in sys.modules:
            importlib.reload(sys.modules[name])
        else:
            importlib.import_module(name)
    from app.main import app

    return TestClient(app)


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.parametrize("name", SETS)
def test_an_arc_links_to_its_session_lesson_and_decision_through_typed_relations(tmp_path, name):
    fixture = FIXTURE[name]
    store = _loaded(tmp_path, fixture)
    arc = next(e for e in fixture["entities"] if e["type"] == "arc")

    views = {(v["name"], v["other"]) for v in store.relations(arc["id"])}

    session, lesson, decision = (
        next(e["id"] for e in fixture["entities"] if e["type"] == t)
        for t in ("session", "lesson", "decision")
    )
    assert views == {
        ("derived_from", session),
        ("has_lesson", lesson),
        ("applies_decision", decision),
    }
    link = next(v for v in store.relations(arc["id"]) if v["name"] == "has_lesson")
    assert link["direction"] == "out"
    assert link["status"] == "accepted"
    assert link["confidence"] == "extracted"
    assert link["evidence"] == fixture["valid"][0]["evidence"]


@pytest.mark.parametrize("name", SETS)
def test_the_target_sees_the_inverse_name_of_the_same_edge(tmp_path, name):
    fixture = FIXTURE[name]
    store = _loaded(tmp_path, fixture)
    lesson = fixture["valid"][0]["target"]

    back = [v for v in store.relations(lesson) if v["other"] == fixture["valid"][0]["source"]]

    assert len(back) == 1
    assert back[0]["name"] == "lesson_of"
    assert back[0]["type"] == "has_lesson"
    assert back[0]["direction"] == "in"


@pytest.mark.parametrize("name", SETS)
def test_an_inverse_pair_is_stored_as_one_canonical_edge(tmp_path, name):
    fixture = FIXTURE[name]
    store = _loaded(tmp_path, fixture)
    before = store.snapshot()

    result = store.put_relation(fixture["inverse"])

    assert result["action"] == "replayed"
    assert result["relation"]["source"] == fixture["valid"][0]["source"]
    assert result["relation"]["type"] == "has_lesson"
    assert result["relation"]["target"] == fixture["valid"][0]["target"]
    assert store.snapshot() == before


@pytest.mark.parametrize("name", SETS)
def test_entities_keep_their_vault_reference_and_revisioned_provenance(tmp_path, name):
    fixture = FIXTURE[name]
    store = _loaded(tmp_path, fixture)

    for entity in fixture["entities"]:
        stored = store.get_entity(entity["id"])
        assert stored["type"] == entity["type"]
        assert stored["vault_ref"] == entity["vault_ref"]
        assert stored["provenance"] == entity["provenance"]
        assert stored["revision"] == entity["revision"]
        assert stored["projects"] == entity["projects"]
        assert stored["title"] == entity["title"]


def test_entity_ids_are_typed_and_stable():
    graph = _graph()

    assert graph.entity_id("arc", "alpha-arc-1") == "arc:alpha-arc-1"
    with pytest.raises(graph.GraphRejection) as unknown:
        graph.entity_id("note", "x")
    assert unknown.value.reason == "unknown_entity_type"
    with pytest.raises(graph.GraphRejection) as blank:
        graph.entity_id("arc", " ")
    assert blank.value.reason == "invalid_entity"


def test_an_entity_whose_id_does_not_name_its_type_is_rejected(tmp_path):
    store = _store(tmp_path)
    entity = dict(FIXTURE["alpha"]["entities"][0], type="lesson")

    with pytest.raises(_graph().GraphRejection) as exc:
        store.put_entity(entity)

    assert exc.value.reason == "invalid_entity"
    assert store.get_entity(entity["id"]) is None


def test_relation_schema_names_inverse_and_endpoint_types():
    schema = _graph().RELATION_TYPES

    assert schema["has_lesson"].inverse == "lesson_of"
    assert schema["has_lesson"].sources == frozenset({"arc"})
    assert schema["has_lesson"].targets == frozenset({"lesson"})
    assert schema["derived_from"].targets == frozenset({"session"})
    assert schema["applies_decision"].targets == frozenset({"decision"})
    assert schema["supported_by"].sources == frozenset({"arc", "lesson", "decision"})
    assert schema["supported_by"].targets == frozenset({"evidence"})
    inverses = [t.inverse for t in schema.values()]
    assert len(set(inverses)) == len(inverses)
    assert not set(inverses) & set(schema)


@pytest.mark.parametrize("name", SETS)
@pytest.mark.parametrize(
    ("case", "reason"),
    [("missing_target", "unknown_endpoint"), ("incompatible", "incompatible_endpoint")],
)
def test_a_bad_relation_is_rejected_without_touching_stored_edges(tmp_path, name, case, reason):
    fixture = FIXTURE[name]
    store = _loaded(tmp_path, fixture)
    before = store.snapshot()

    with pytest.raises(_graph().GraphRejection) as exc:
        store.put_relation(fixture[case])

    assert exc.value.reason == reason
    assert store.snapshot() == before
    assert store.invalid_relations_total() == {reason: 1}
    assert store.quarantined() == []


@pytest.mark.parametrize("name", SETS)
def test_a_bad_relation_can_be_quarantined_for_inspection(tmp_path, name):
    fixture = FIXTURE[name]
    store = _loaded(tmp_path, fixture)
    before = store.snapshot()

    result = store.put_relation(fixture["incompatible"], quarantine=True)

    assert result == {"action": "quarantined", "reason": "incompatible_endpoint"}
    assert store.snapshot() == before
    held = store.quarantined()
    assert [q["reason"] for q in held] == ["incompatible_endpoint"]
    assert held[0]["record"] == fixture["incompatible"]
    assert store.invalid_relations_total() == {"incompatible_endpoint": 1}


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("type", "befriends", "unknown_relation_type"),
        ("status", "maybe", "invalid_relation"),
        ("confidence", "certain", "invalid_relation"),
        ("evidence", "evidence:alpha-run-1", "invalid_relation"),
        ("source", "arc:alpha-missing", "unknown_endpoint"),
    ],
)
def test_unknown_types_and_fields_are_rejected(tmp_path, field, value, reason):
    store = _loaded(tmp_path, FIXTURE["alpha"])
    before = store.snapshot()
    bad = dict(FIXTURE["alpha"]["valid"][0], **{field: value})

    with pytest.raises(_graph().GraphRejection) as exc:
        store.put_relation(bad)

    assert exc.value.reason == reason
    assert store.snapshot() == before


def test_correcting_a_rejected_relation_needs_a_new_valid_request(tmp_path):
    fixture = FIXTURE["alpha"]
    store = _loaded(tmp_path, fixture)
    store.put_relation(fixture["missing_target"], quarantine=True)
    missing = fixture["missing_target"]["target"]

    store.put_entity(
        dict(fixture["entities"][2], id=missing, provenance={"source": "marker:x", "revision": "m"})
    )

    assert all(v["other"] != missing for v in store.relations(fixture["valid"][0]["source"]))
    assert store.put_relation(fixture["missing_target"])["action"] == "created"


def test_a_replayed_relation_leaves_one_edge_and_a_changed_one_updates_it(tmp_path):
    fixture = FIXTURE["alpha"]
    store = _loaded(tmp_path, fixture)
    count = len(store.snapshot()[1])

    assert store.put_relation(fixture["valid"][2])["action"] == "replayed"
    changed = dict(
        fixture["valid"][2], revision=2, status="accepted", evidence=["evidence:alpha-run-1"]
    )
    assert store.put_relation(changed)["action"] == "updated"

    assert len(store.snapshot()[1]) == count
    view = next(v for v in store.relations(changed["source"]) if v["name"] == "applies_decision")
    assert view["revision"] == 2
    assert view["status"] == "accepted"
    assert view["evidence"] == ["evidence:alpha-run-1"]


def test_an_outdated_relation_revision_cannot_overwrite_a_newer_one(tmp_path):
    fixture = FIXTURE["alpha"]
    store = _loaded(tmp_path, fixture)
    newer = dict(fixture["valid"][2], revision=3, status="accepted")
    assert store.put_relation(newer)["action"] == "updated"
    before = store.snapshot()

    older = dict(fixture["valid"][2], revision=2, status="retired")
    same_number = dict(newer, status="retired")

    assert store.put_relation(older)["action"] == "stale"
    assert store.put_relation(same_number)["action"] == "stale"
    assert store.snapshot() == before


def test_rebuild_keeps_the_newest_relation_revision_in_any_order(tmp_path):
    fixture = FIXTURE["alpha"]
    newer = dict(fixture["valid"][2], revision=2, status="accepted")
    forward = {"entities": fixture["entities"], "relations": [*fixture["valid"], newer]}
    backward = {"entities": fixture["entities"], "relations": [newer, *fixture["valid"]]}
    first, second = _store(tmp_path, "a.db"), _store(tmp_path, "b.db")

    first.rebuild(forward)
    second.rebuild(backward)

    assert first.snapshot() == second.snapshot()
    view = next(v for v in first.relations(newer["source"]) if v["name"] == "applies_decision")
    assert (view["revision"], view["status"]) == (2, "accepted")


def test_a_stale_entity_revision_cannot_overwrite_a_newer_one(tmp_path):
    store = _loaded(tmp_path, FIXTURE["alpha"])
    arc = FIXTURE["alpha"]["entities"][0]
    newer = dict(arc, revision=3, title="Alpha ingest retries, revised")

    assert store.put_entity(newer) == "updated"
    assert store.put_entity(dict(arc, revision=2, title="older")) == "stale"
    assert store.put_entity(newer) == "replayed"

    assert store.get_entity(arc["id"])["title"] == newer["title"]
    assert store.get_entity(arc["id"])["revision"] == 3


def test_revising_a_linked_entity_keeps_its_edges(tmp_path):
    fixture = FIXTURE["alpha"]
    store = _loaded(tmp_path, fixture)
    edges = store.snapshot()[1]

    assert store.put_entity(dict(fixture["entities"][2], revision=2, title="revised")) == "updated"

    assert store.snapshot()[1] == edges


@pytest.mark.parametrize(
    ("field", "value"),
    [("type", ["has_lesson"]), ("status", {"a": 1}), ("confidence", ["observed"])],
)
def test_non_string_relation_fields_are_rejected_not_crashed(
    monkeypatch, vault, tmp_path, field, value
):
    client = _client(monkeypatch, vault, tmp_path / "graph.db")
    _seed(client, FIXTURE["alpha"])
    bad = dict(FIXTURE["alpha"]["valid"][0], **{field: value})

    response = client.post("/graph/relation", json=bad, headers=_auth(ROUTER))

    assert response.status_code == 422


def test_a_non_string_entity_type_is_rejected_not_crashed(monkeypatch, vault, tmp_path):
    client = _client(monkeypatch, vault, tmp_path / "graph.db")
    entity = dict(FIXTURE["alpha"]["entities"][0], type=["arc"])

    response = client.post("/graph/entity", json=entity, headers=_auth(ROUTER))

    assert response.status_code == 422
    assert response.json()["detail"]["reason"] == "unknown_entity_type"


def test_rebuild_reports_rejects_without_raising_the_live_counter(tmp_path):
    fixture = FIXTURE["alpha"]
    store = _store(tmp_path)

    for _ in range(2):
        report = store.rebuild(
            {"entities": fixture["entities"], "relations": [fixture["incompatible"]]}
        )

    assert report["rejected"] == {"incompatible_endpoint": 1}
    assert store.invalid_relations_total() == {}


@pytest.mark.parametrize("name", SETS)
def test_rebuild_from_canonical_metadata_yields_the_same_entities_and_edges(tmp_path, name):
    fixture = FIXTURE[name]
    live = _loaded(tmp_path, fixture, "live.db")
    canonical = {
        "entities": fixture["entities"],
        "relations": [*fixture["valid"], fixture["inverse"], fixture["incompatible"]],
    }

    rebuilt = _store(tmp_path, "rebuilt.db")
    report = rebuilt.rebuild(canonical)

    assert rebuilt.snapshot() == live.snapshot()
    assert report == {
        "entities": len(fixture["entities"]),
        "relations": len(fixture["valid"]),
        "rejected": {"incompatible_endpoint": 1},
    }


def test_rebuild_replaces_a_drifted_projection_and_is_repeatable(tmp_path):
    fixture = FIXTURE["alpha"]
    canonical = {"entities": fixture["entities"], "relations": fixture["valid"]}
    store = _store(tmp_path)
    store.rebuild(canonical)
    expected = store.snapshot()
    store.put_entity(dict(FIXTURE["beta"]["entities"][0]))

    store.rebuild(canonical)
    store.rebuild(copy.deepcopy(canonical))

    assert store.snapshot() == expected


def test_rebuild_keeps_the_newest_revision_of_a_duplicated_entity(tmp_path):
    arc = FIXTURE["alpha"]["entities"][0]
    store = _store(tmp_path)

    store.rebuild(
        {"entities": [dict(arc, revision=4, title="new"), dict(arc, revision=2)], "relations": []}
    )

    assert store.get_entity(arc["id"])["title"] == "new"


def test_rebuild_with_a_bad_entity_leaves_the_projection_untouched(tmp_path):
    fixture = FIXTURE["alpha"]
    store = _loaded(tmp_path, fixture)
    before = store.snapshot()

    with pytest.raises(_graph().GraphRejection):
        store.rebuild({"entities": [dict(fixture["entities"][0], type="note")], "relations": []})

    assert store.snapshot() == before


def test_the_projection_survives_a_restart_of_the_owning_process(tmp_path):
    fixture = FIXTURE["alpha"]
    before = _loaded(tmp_path, fixture).snapshot()

    reopened = _store(tmp_path)

    assert reopened.snapshot() == before
    assert reopened.put_relation(fixture["valid"][0])["action"] == "replayed"


def test_the_graph_is_inactive_without_its_database_setting(monkeypatch, vault):
    client = _client(monkeypatch, vault, None)

    response = client.get("/graph/entity/arc:alpha-arc-1", headers=_auth(ROUTER))

    assert response.status_code == 404
    assert response.json()["detail"] == "knowledge graph is inactive"


def _seed(client, fixture, token=ROUTER):
    for entity in fixture["entities"]:
        assert client.post("/graph/entity", json=entity, headers=_auth(token)).status_code == 201
    for relation in fixture["valid"]:
        assert (
            client.post("/graph/relation", json=relation, headers=_auth(token)).status_code == 201
        )


def test_http_writes_and_reads_typed_relations(monkeypatch, vault, tmp_path):
    fixture = FIXTURE["alpha"]
    client = _client(monkeypatch, vault, tmp_path / "graph.db")
    _seed(client, fixture)

    body = client.get(f"/graph/entity/{fixture['entities'][0]['id']}", headers=_auth(ROUTER)).json()

    assert body["entity"]["vault_ref"] == fixture["entities"][0]["vault_ref"]
    assert {(r["name"], r["other"]) for r in body["relations"]} == {
        (r["type"], r["target"]) for r in fixture["valid"][:3]
    }
    replay = client.post("/graph/relation", json=fixture["inverse"], headers=_auth(ROUTER))
    assert replay.json()["action"] == "replayed"


def test_http_rejects_a_bad_relation_and_counts_it(monkeypatch, vault, tmp_path):
    fixture = FIXTURE["alpha"]
    client = _client(monkeypatch, vault, tmp_path / "graph.db")
    _seed(client, fixture)

    rejected = client.post("/graph/relation", json=fixture["missing_target"], headers=_auth(ROUTER))
    held = client.post(
        "/graph/relation?quarantine=true", json=fixture["incompatible"], headers=_auth(ROUTER)
    )
    stats = client.get("/graph/stats", headers=_auth(ROUTER)).json()

    assert rejected.status_code == 422
    assert rejected.json()["detail"] == {
        "reason": "unknown_endpoint",
        "graph_invalid_relations_total": 1,
    }
    assert held.status_code == 202
    assert held.json()["reason"] == "incompatible_endpoint"
    assert stats["graph_invalid_relations_total"] == {
        "unknown_endpoint": 1,
        "incompatible_endpoint": 1,
    }
    assert stats["quarantined"] == 1


def test_a_scoped_worker_sees_and_links_only_its_own_project(monkeypatch, vault, tmp_path):
    client = _client(monkeypatch, vault, tmp_path / "graph.db", "hive-a@proj-alpha=alpha-token")
    _seed(client, FIXTURE["alpha"])
    _seed(client, FIXTURE["beta"])
    alpha_arc = FIXTURE["alpha"]["entities"][0]["id"]
    beta_lesson = FIXTURE["beta"]["entities"][2]["id"]
    worker = _auth("alpha-token")

    assert client.get(f"/graph/entity/{alpha_arc}", headers=worker).status_code == 200
    assert client.get(f"/graph/entity/{beta_lesson}", headers=worker).status_code == 404
    cross = client.post(
        "/graph/relation",
        json=dict(FIXTURE["alpha"]["valid"][0], target=beta_lesson),
        headers=worker,
    )
    assert cross.status_code == 422
    assert cross.json()["detail"]["reason"] == "unknown_endpoint"
    foreign = client.post("/graph/entity", json=FIXTURE["beta"]["entities"][0], headers=worker)
    assert foreign.status_code == 403
    router_view = client.get(f"/graph/entity/{alpha_arc}", headers=_auth(ROUTER)).json()
    assert all(r["other"] != beta_lesson for r in router_view["relations"])


def test_a_relation_to_a_hidden_entity_is_omitted_from_a_scoped_read(monkeypatch, vault, tmp_path):
    client = _client(monkeypatch, vault, tmp_path / "graph.db", "hive-a@proj-alpha=alpha-token")
    _seed(client, FIXTURE["alpha"])
    _seed(client, FIXTURE["beta"])
    beta_evidence = FIXTURE["beta"]["entities"][4]["id"]
    alpha_lesson = FIXTURE["alpha"]["entities"][2]["id"]
    link = {
        "source": alpha_lesson,
        "type": "supported_by",
        "target": beta_evidence,
        "status": "accepted",
        "confidence": "observed",
        "evidence": [],
    }
    assert client.post("/graph/relation", json=link, headers=_auth(ROUTER)).status_code == 201

    scoped = client.get(f"/graph/entity/{alpha_lesson}", headers=_auth("alpha-token")).json()
    full = client.get(f"/graph/entity/{alpha_lesson}", headers=_auth(ROUTER)).json()

    assert beta_evidence not in {r["other"] for r in scoped["relations"]}
    assert beta_evidence in {r["other"] for r in full["relations"]}


def test_a_scoped_worker_cannot_take_over_a_foreign_entity(monkeypatch, vault, tmp_path):
    client = _client(monkeypatch, vault, tmp_path / "graph.db", "hive-a@proj-alpha=alpha-token")
    _seed(client, FIXTURE["beta"])
    beta_arc = FIXTURE["beta"]["entities"][0]
    takeover = dict(beta_arc, projects=["proj-alpha"], revision=beta_arc["revision"] + 1)

    response = client.post("/graph/entity", json=takeover, headers=_auth("alpha-token"))
    stored = client.get(f"/graph/entity/{beta_arc['id']}", headers=_auth(ROUTER)).json()

    assert response.status_code == 403
    assert stored["entity"]["projects"] == ["proj-beta"]
    assert stored["entity"]["revision"] == beta_arc["revision"]


def test_a_scoped_worker_cannot_write_an_entity_with_no_project(monkeypatch, vault, tmp_path):
    client = _client(monkeypatch, vault, tmp_path / "graph.db", "hive-a@proj-alpha=alpha-token")
    unscoped = dict(FIXTURE["alpha"]["entities"][0], projects=[])

    response = client.post("/graph/entity", json=unscoped, headers=_auth("alpha-token"))

    assert response.status_code == 403
    assert client.get(f"/graph/entity/{unscoped['id']}", headers=_auth(ROUTER)).status_code == 404


def test_http_refuses_an_outdated_relation_revision(monkeypatch, vault, tmp_path):
    fixture = FIXTURE["alpha"]
    client = _client(monkeypatch, vault, tmp_path / "graph.db")
    _seed(client, fixture)
    newer = dict(fixture["valid"][0], revision=2, status="retired")
    assert client.post("/graph/relation", json=newer, headers=_auth(ROUTER)).status_code == 201

    stale = client.post("/graph/relation", json=fixture["valid"][0], headers=_auth(ROUTER))
    body = client.get(f"/graph/entity/{newer['source']}", headers=_auth(ROUTER)).json()

    assert stale.status_code == 409
    view = next(r for r in body["relations"] if r["other"] == newer["target"])
    assert (view["revision"], view["status"]) == (2, "retired")


def test_http_rejects_entity_projects_that_are_not_ids(monkeypatch, vault, tmp_path):
    client = _client(monkeypatch, vault, tmp_path / "graph.db")
    bad = dict(FIXTURE["alpha"]["entities"][0], projects=[["proj-alpha"]])

    response = client.post("/graph/entity", json=bad, headers=_auth(ROUTER))

    assert response.status_code == 422
    assert response.json()["detail"]["reason"] == "invalid_entity"
