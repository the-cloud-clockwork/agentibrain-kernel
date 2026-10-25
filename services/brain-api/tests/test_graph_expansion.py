import importlib

import pytest

from tests.test_graph import ROUTER, _auth, _client


def _fixture(client):
    entities = [
        ("session:seed", "session", "alpha", "Scoped seed"),
        ("arc:bridge", "arc", "alpha", "Connected history"),
        ("lesson:answer", "lesson", "alpha", "Use bounded retries"),
        ("arc:hidden", "arc", "beta", "Forbidden title"),
        ("lesson:hidden", "lesson", "beta", "Forbidden lesson"),
    ]
    for ident, kind, project, title in entities:
        entity = {
            "id": ident,
            "type": kind,
            "revision": 1,
            "projects": [project],
            "vault_ref": None,
            "title": title,
            "provenance": {"source": "fixture", "revision": "1"},
        }
        assert client.post("/graph/entity", json=entity, headers=_auth(ROUTER)).status_code == 201
    edges = [
        ("arc:bridge", "derived_from", "session:seed"),
        ("arc:bridge", "has_lesson", "lesson:answer"),
        ("arc:hidden", "derived_from", "session:seed"),
        ("arc:hidden", "has_lesson", "lesson:hidden"),
    ]
    for source, kind, target in edges:
        relation = {
            "source": source,
            "type": kind,
            "target": target,
            "status": "accepted",
            "confidence": "observed",
            "evidence": [],
        }
        assert (
            client.post("/graph/relation", json=relation, headers=_auth(ROUTER)).status_code == 201
        )


def _setup(monkeypatch, vault, tmp_path):
    client = _client(monkeypatch, vault, tmp_path / "graph.db", "worker=hive-a@alpha")
    monkeypatch.setenv("BRAIN_GRAPH_EXPANSION", "true")
    _fixture(client)
    return client


@pytest.mark.parametrize("independent_fixture", [1, 2])
def test_authorized_two_hop_lesson_has_a_directional_path(
    monkeypatch, vault, tmp_path, independent_fixture
):
    client = _setup(monkeypatch, vault, tmp_path)
    before = importlib.import_module("app.main")._graph_store().snapshot()
    response = client.post(
        "/graph/expand", json={"seed_ids": ["session:seed"]}, headers=_auth("worker")
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["partial"] is False
    assert [node["id"] for node in result["nodes"]] == [
        "session:seed",
        "arc:bridge",
        "lesson:answer",
    ]
    lesson = result["nodes"][-1]
    assert [(edge["source"], edge["type"], edge["target"]) for edge in lesson["path"]] == [
        ("arc:bridge", "derived_from", "session:seed"),
        ("arc:bridge", "has_lesson", "lesson:answer"),
    ]
    assert lesson["explanation"] == "Connected to session:seed by 2 accepted relations"
    assert result["graph_nodes_expanded_per_request"] == 3
    assert "Forbidden" not in response.text
    assert ":hidden" not in response.text
    assert importlib.import_module("app.main")._graph_store().snapshot() == before
