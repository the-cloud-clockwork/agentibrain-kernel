import importlib

import pytest

from tests.test_graph import ROUTER, _auth, _client


def _setup(monkeypatch, vault, tmp_path):
    client = _client(monkeypatch, vault, tmp_path / "graph.db", "hive-a@alpha=worker")
    monkeypatch.setenv("BRAIN_GRAPH_EXPANSION", "true")
    for ident, project in [
        ("lesson:old", "alpha"),
        ("lesson:opposite", "alpha"),
        ("decision:approved", "alpha"),
        ("decision:hidden", "beta"),
    ]:
        record = {
            "id": ident,
            "type": ident.split(":")[0],
            "revision": 1,
            "projects": [project],
            "vault_ref": ident,
            "title": ident,
            "provenance": {"source": "fixture", "revision": "1"},
        }
        assert client.post("/graph/entity", json=record, headers=_auth(ROUTER)).status_code == 201
    return client


def _edge(client, source, kind, target, **changes):
    record = {
        "source": source,
        "type": kind,
        "target": target,
        "status": "accepted",
        "confidence": "observed",
        "evidence": ["review:fixture"],
        **changes,
    }
    return client.post("/graph/relation", json=record, headers=_auth(ROUTER))


def _read(client, **changes):
    response = client.post(
        "/graph/expand", json={"seed_ids": ["lesson:old"], **changes}, headers=_auth("worker")
    )
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize("independent_fixture", [1, 2])
def test_approved_replacement_retains_cited_history(
    monkeypatch, vault, tmp_path, independent_fixture
):
    client = _setup(monkeypatch, vault, tmp_path)
    assert _edge(client, "lesson:old", "contradicts", "lesson:opposite").status_code == 201
    for lesson in ("lesson:old", "lesson:opposite"):
        assert _edge(client, "decision:approved", "supersedes", lesson).status_code == 201
    store = importlib.import_module("app.main")._graph_store()
    before = store.snapshot()
    result = _read(client)
    assert [node["id"] for node in result["guidance"]] == ["decision:approved"]
    history = {node["id"]: node for node in result["history"]}
    assert set(history) == {"lesson:old", "lesson:opposite"}
    assert history["lesson:old"]["knowledge_status"] == "superseded"
    assert history["lesson:old"]["superseded_by"] == ["decision:approved"]
    assert history["lesson:old"]["vault_ref"] == "lesson:old"
    assert result["unresolved_knowledge_conflicts"] == 0
    assert store.snapshot() == before


def test_unresolved_contradictions_are_explicit(monkeypatch, vault, tmp_path):
    client = _setup(monkeypatch, vault, tmp_path)
    assert _edge(client, "lesson:old", "contradicts", "lesson:opposite").status_code == 201
    result = _read(client)
    assert result["guidance"] == []
    assert result["unresolved_knowledge_conflicts"] == 1
    assert result["conflicts"][0]["kind"] == "contradiction"
    assert {node["knowledge_status"] for node in result["nodes"]} == {"conflicted"}


def test_cycle_has_no_current_guidance_and_reviewed_retirement_recovers(
    monkeypatch, vault, tmp_path
):
    client = _setup(monkeypatch, vault, tmp_path)
    assert _edge(client, "lesson:old", "supersedes", "lesson:opposite").status_code == 201
    assert _edge(client, "lesson:opposite", "supersedes", "lesson:old").status_code == 201
    store = importlib.import_module("app.main")._graph_store()
    before = store.snapshot()
    result = _read(client)
    assert result["guidance"] == []
    assert result["conflicts"][0]["kind"] == "supersession_cycle"
    assert store.snapshot() == before
    assert (
        _edge(
            client, "lesson:opposite", "supersedes", "lesson:old", status="retired", revision=2
        ).status_code
        == 201
    )
    recovered = _read(client)
    assert [node["id"] for node in recovered["guidance"]] == ["lesson:old"]
    assert _edge(client, "lesson:opposite", "supersedes", "lesson:old").status_code == 409
    assert _read(client) == recovered
    assert (
        _edge(
            client, "lesson:opposite", "supersedes", "lesson:old", status="retired", revision=2
        ).status_code
        == 201
    )
    assert _read(client) == recovered


def test_hidden_replacement_cannot_change_authorized_guidance(monkeypatch, vault, tmp_path):
    client = _setup(monkeypatch, vault, tmp_path)
    assert _edge(client, "decision:hidden", "supersedes", "lesson:old").status_code == 201
    result = _read(client)
    assert "hidden" not in str(result)
    assert [node["id"] for node in result["guidance"]] == ["lesson:old"]


@pytest.mark.parametrize("changes", [{"status": "proposed"}, {"confidence": "inferred"}])
def test_unapproved_replacement_does_not_suppress_history(monkeypatch, vault, tmp_path, changes):
    client = _setup(monkeypatch, vault, tmp_path)
    assert (
        _edge(client, "decision:approved", "supersedes", "lesson:old", **changes).status_code == 201
    )
    result = _read(client)
    old = next(node for node in result["nodes"] if node["id"] == "lesson:old")
    assert old["knowledge_status"] == "current"


def test_rollback_displays_conflicts_without_automatic_guidance(monkeypatch, vault, tmp_path):
    client = _setup(monkeypatch, vault, tmp_path)
    assert _edge(client, "lesson:old", "contradicts", "lesson:opposite").status_code == 201
    before = importlib.import_module("app.main")._graph_store().snapshot()
    monkeypatch.setenv("BRAIN_CURRENT_KNOWLEDGE", "false")
    result = _read(client)
    assert result["guidance"] == []
    assert result["selection_enabled"] is False
    assert result["unresolved_knowledge_conflicts"] == 1
    assert importlib.import_module("app.main")._graph_store().snapshot() == before
