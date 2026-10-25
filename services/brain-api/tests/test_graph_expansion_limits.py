import importlib
import time
from types import SimpleNamespace

import pytest

from tests.test_graph import ROUTER, _auth
from tests.test_graph_expansion import _setup


def _entity(client, ident, kind="arc", projects=None):
    body = {
        "id": ident,
        "type": kind,
        "projects": projects or ["alpha"],
        "revision": 1,
        "vault_ref": None,
        "title": ident,
        "provenance": {"source": "fixture", "revision": "1"},
    }
    assert client.post("/graph/entity", json=body, headers=_auth(ROUTER)).status_code == 201


def _edge(client, source, kind, target, status="accepted"):
    body = {
        "source": source,
        "type": kind,
        "target": target,
        "status": status,
        "confidence": "observed",
        "evidence": [],
    }
    assert client.post("/graph/relation", json=body, headers=_auth(ROUTER)).status_code == 201


def _expand(client, **kwargs):
    return client.post(
        "/graph/expand", json={"seed_ids": ["session:seed"], **kwargs}, headers=_auth("worker")
    )


def test_cycle_terminates_and_each_authorized_node_appears_once(monkeypatch, vault, tmp_path):
    client = _setup(monkeypatch, vault, tmp_path)
    _entity(client, "evidence:cycle", "evidence")
    _entity(client, "arc:cycle")
    _edge(client, "lesson:answer", "supported_by", "evidence:cycle")
    _edge(client, "arc:cycle", "supported_by", "evidence:cycle")
    _edge(client, "arc:cycle", "derived_from", "session:seed")
    data = _expand(client, max_depth=4).json()
    assert data["partial"] is False
    assert len(data["nodes"]) == 5
    assert len({node["id"] for node in data["nodes"]}) == 5
    assert data["edges_examined"] < 20


def test_high_degree_hub_stops_at_node_and_edge_budgets(monkeypatch, vault, tmp_path):
    client = _setup(monkeypatch, vault, tmp_path)
    for index in range(80):
        ident = f"arc:hub{index:03}"
        _entity(client, ident)
        _edge(client, ident, "derived_from", "session:seed")
    data = _expand(client, max_nodes=5).json()
    assert data["partial"] is True
    assert data["reason"] == "node_cap"
    assert len(data["nodes"]) == 5
    assert data["edges_examined"] <= 6
    assert data["continuation"] is None
    data = _expand(client, max_edges=2).json()
    assert data["reason"] == "edge_cap"
    assert data["edges_examined"] == 2
    assert "Forbidden" not in str(data)


def test_depth_and_allowed_relations_bound_relevance(monkeypatch, vault, tmp_path):
    client = _setup(monkeypatch, vault, tmp_path)
    data = _expand(client, max_depth=1).json()
    assert data["reason"] == "depth_cap"
    assert [n["id"] for n in data["nodes"]] == ["session:seed", "arc:bridge"]
    data = _expand(client, relation_types=["derived_from"]).json()
    assert data["partial"] is False
    assert [n["id"] for n in data["nodes"]] == ["session:seed", "arc:bridge"]


def test_unknown_and_forbidden_seeds_have_identical_errors(monkeypatch, vault, tmp_path):
    client = _setup(monkeypatch, vault, tmp_path)
    missing = _expand(client, seed_ids=["arc:absent"])
    hidden = _expand(client, seed_ids=["arc:hidden"])
    assert missing.status_code == hidden.status_code == 404
    assert missing.json() == hidden.json() == {"detail": "seed not found"}
    assert client.post("/graph/expand", json={"seed_ids": ["session:seed"]}).status_code == 401


def test_proposed_edges_and_hidden_intermediates_never_expand(monkeypatch, vault, tmp_path):
    client = _setup(monkeypatch, vault, tmp_path)
    _entity(client, "arc:proposed")
    _edge(client, "arc:proposed", "derived_from", "session:seed", status="proposed")
    _entity(client, "lesson:behind-hidden", "lesson")
    _edge(client, "arc:hidden", "has_lesson", "lesson:behind-hidden")
    data = _expand(client, max_depth=4).json()
    assert [n["id"] for n in data["nodes"]] == ["session:seed", "arc:bridge", "lesson:answer"]


def test_a_slow_runner_cannot_empty_an_expansion(monkeypatch, vault, tmp_path):
    client = _setup(monkeypatch, vault, tmp_path)
    graph = importlib.import_module("app.graph")
    original = graph.GraphReadView.get_entity

    def slow(view, ident):
        time.sleep(0.15)
        return original(view, ident)

    monkeypatch.setattr(graph.GraphReadView, "get_entity", slow)
    data = _expand(client).json()
    assert data["partial"] is False
    assert [n["id"] for n in data["nodes"]] == ["session:seed", "arc:bridge", "lesson:answer"]


def _timeout(monkeypatch, client):
    retrieval = importlib.import_module("app.retrieval.graph")
    graph = importlib.import_module("app.graph")
    clock = SimpleNamespace(now=time.monotonic(), fired=False)
    timer = SimpleNamespace(monotonic=lambda: clock.now)
    monkeypatch.setattr(retrieval, "time", timer)
    monkeypatch.setattr(graph, "time", timer)
    original = graph.GraphReadView.get_entity

    def read(view, ident):
        entity = original(view, ident)
        if ident == "arc:bridge" and not clock.fired:
            clock.now += 1
            clock.fired = True
        return entity

    monkeypatch.setattr(graph.GraphReadView, "get_entity", read)
    result = _expand(client).json()
    assert result["reason"] == "timeout"
    assert result["partial"] is True
    assert [n["id"] for n in result["nodes"]] == ["session:seed", "arc:bridge"]
    assert result["continuation"]
    return result, clock


def test_timeout_resumes_with_evidence_and_without_duplicate_nodes(monkeypatch, vault, tmp_path):
    client = _setup(monkeypatch, vault, tmp_path)
    partial, _ = _timeout(monkeypatch, client)
    response = _expand(client, continuation=partial["continuation"])
    assert response.status_code == 200
    result = response.json()
    assert result["partial"] is False
    assert [n["id"] for n in result["nodes"]] == ["session:seed", "arc:bridge", "lesson:answer"]
    assert len(result["nodes"][-1]["path"]) == 2
    assert result["graph_nodes_expanded_per_request"] == 1
    assert _expand(client, continuation=partial["continuation"]).status_code == 409


@pytest.mark.parametrize("change", ["grant", "project", "actor", "revision", "expiry", "forged"])
def test_continuations_refuse_changed_authority_or_evidence(monkeypatch, vault, tmp_path, change):
    client = _setup(monkeypatch, vault, tmp_path)
    partial, clock = _timeout(monkeypatch, client)
    token = partial["continuation"]
    body = {"continuation": token}
    actor = "worker"
    if change == "grant":
        importlib.import_module("app.scope").PROJECT_GRANTS["worker"] = frozenset({"beta"})
    elif change == "project":
        body["project"] = "beta"
    elif change == "actor":
        actor = ROUTER
    elif change == "revision":
        _entity(client, "arc:new")
    elif change == "expiry":
        clock.now += 61
    else:
        body["continuation"] = "forged"
    response = client.post("/graph/expand", json=body, headers=_auth(actor))
    assert response.status_code == 409
    assert "Forbidden" not in response.text


def test_rollback_disables_expansion_and_retains_scoped_seeds(monkeypatch, vault, tmp_path):
    client = _setup(monkeypatch, vault, tmp_path)
    monkeypatch.setenv("BRAIN_GRAPH_EXPANSION", "false")
    result = _expand(client).json()
    assert result["reason"] == "disabled"
    assert result["partial"] is False
    assert [n["id"] for n in result["nodes"]] == ["session:seed"]
    assert result["continuation"] is None
    assert result["edges_examined"] == 0


@pytest.mark.parametrize(
    "field,value",
    [
        ("max_depth", 5),
        ("max_nodes", 101),
        ("max_edges", 1001),
        ("time_budget_ms", 501),
        ("relation_types", ["invented"]),
    ],
)
def test_budgets_and_relation_types_are_validated(monkeypatch, vault, tmp_path, field, value):
    client = _setup(monkeypatch, vault, tmp_path)
    assert _expand(client, **{field: value}).status_code == 422
