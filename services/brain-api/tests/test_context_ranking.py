import importlib
import json
import sqlite3
from pathlib import Path

import pytest

from .test_context_request import TOKENS, _post, _request
from .test_graph import _client

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "sv2_ctx02_ranking.json").read_text())
SETS = ["first", "second"]


def _rank():
    return importlib.import_module("app.retrieval.rank")


def _source(values):
    return lambda _query, ids: {ident: values.get(ident, 0.0) for ident in ids}


def _failing(_query, _ids):
    raise ConnectionError("embeddings unreachable")


def _loaded(db: Path, fixture: dict):
    store = importlib.import_module("app.graph").GraphStore(db)
    for entity in fixture["entities"]:
        store.put_entity(entity)
    for relation in fixture["relations"]:
        store.put_relation(relation)
    return store


def _context(monkeypatch, vault, tmp_path, name, semantic="fixture", **env):
    fixture = FIXTURE[name]
    db = tmp_path / f"{name}.db"
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    client = _client(monkeypatch, vault, db, TOKENS)
    store = _loaded(db, fixture)
    _sources(monkeypatch, fixture, semantic)
    return client, store, fixture


def _sources(monkeypatch, fixture, semantic):
    ctx = importlib.import_module("app.context")
    sources = {"fixture": _source(fixture["semantic"]), "failing": _failing, None: None}
    monkeypatch.setattr(ctx, "semantic_source", sources[semantic])
    monkeypatch.setattr(ctx, "heat_source", _source(fixture["heat"]))


def _ask(client, fixture, **changes):
    return _post(client, _request(**fixture["request"], **changes))


def _ids(context):
    return [item["id"] for item in context["items"]]


@pytest.mark.parametrize("name", SETS)
def test_a_cold_incident_lesson_outranks_an_unrelated_hot_arc(monkeypatch, vault, tmp_path, name):
    client, _, fixture = _context(monkeypatch, vault, tmp_path, name)
    response = _ask(client, fixture)
    assert response.status_code == 200, response.text
    context = response.json()["context"]
    ids = _ids(context)
    assert fixture["cold_incident"] in ids
    assert fixture["hot_arc"] not in ids
    assert context["excluded"]["heat_only"] == 1
    assert context["policy_revision"] == "2"
    assert context["authority"] == "recalled_evidence"
    assert context["sources"] == {"semantic": "ok", "heat": "ok", "graph": "ok"}
    assert context["reduced"] is False
    assert _rank().context_relevance_at_k(context["items"], fixture["relevant"], 3) == 1.0
    incident = context["items"][ids.index(fixture["cold_incident"])]
    assert incident["components"]["heat"] == 0.0
    assert incident["evidence"] == "direct"
    assert incident["confidence"] is None and incident["approval"] is None
    assert any(reason.startswith("matches query terms") for reason in incident["reasons"])


@pytest.mark.parametrize("name", SETS)
def test_the_current_decision_ranks_above_the_decision_it_supersedes(
    monkeypatch, vault, tmp_path, name
):
    client, _, fixture = _context(monkeypatch, vault, tmp_path, name)
    items = {item["id"]: item for item in _ask(client, fixture).json()["context"]["items"]}
    current, superseded = items[fixture["current_decision"]], items[fixture["superseded_decision"]]
    assert current["knowledge_status"] == "current"
    assert superseded["knowledge_status"] == "superseded"
    assert current["score"] > superseded["score"]
    assert "knowledge status current" in current["reasons"]


@pytest.mark.parametrize("name", SETS)
def test_an_uncertain_inferred_edge_is_labelled_and_never_outranks_direct_evidence(
    monkeypatch, vault, tmp_path, name
):
    client, _, fixture = _context(monkeypatch, vault, tmp_path, name)
    items = _ask(client, fixture).json()["context"]["items"]
    neighbor = next(item for item in items if item["id"] == fixture["inferred_neighbor"])
    assert neighbor["evidence"] == "graph"
    assert neighbor["confidence"] == "inferred"
    assert neighbor["approval"] == "accepted"
    assert neighbor["authority"] == "recalled_evidence"
    assert neighbor["components"]["lexical"] == neighbor["components"]["task"] == 0.0
    assert [step["type"] for step in neighbor["path"]] == ["has_lesson"]
    assert neighbor["path"][0]["confidence"] == "inferred"
    direct = [item["score"] for item in items if item["evidence"] == "direct"]
    assert neighbor["score"] < min(direct)


@pytest.mark.parametrize("name", SETS)
def test_heat_alone_cannot_inject_unrelated_memory_and_the_store_is_unchanged(
    monkeypatch, vault, tmp_path, name
):
    client, store, fixture = _context(monkeypatch, vault, tmp_path, name, semantic=None)
    before = store.snapshot()
    response = _ask(client, fixture)
    assert store.snapshot() == before
    text = response.text
    assert fixture["hot_arc"] not in text
    assert fixture["out_of_scope"] not in text
    assert "beta" not in json.dumps(response.json()["context"])
    context = response.json()["context"]
    assert context["sources"]["semantic"] == "not_configured"
    assert context["excluded"] == {"heat_only": 1, "no_task_evidence": 0}
    assert _rank().context_relevance_at_k(context["items"], fixture["relevant"], 3) == 1.0


def test_a_heat_only_candidate_is_excluded_by_the_pure_policy():
    rank = _rank()
    hot = {"id": "arc:hot", "type": "arc", "title": "Unrelated colour work"}
    result = rank.rank(
        rank.Task("transcript ingestion"),
        [hot],
        [],
        rank.policy(rank.CURRENT),
        rank.Sources(heat=_source({"arc:hot": 1.0})),
    )
    assert result["items"] == []
    assert result["excluded"] == {"heat_only": 1, "no_task_evidence": 0}


def test_another_projects_token_cannot_read_this_projects_ranking(monkeypatch, vault, tmp_path):
    client, store, fixture = _context(monkeypatch, vault, tmp_path, "first")
    before = store.snapshot()
    denied = _ask(client, fixture, project_id="beta")
    assert denied.status_code == 403
    assert store.snapshot() == before
    assert denied.json()["detail"]["reason"] == "project_grant"
    assert FIXTURE["first"]["out_of_scope"] not in denied.text


@pytest.mark.parametrize("name", SETS)
def test_a_failed_semantic_source_returns_a_labelled_reduced_result(
    monkeypatch, vault, tmp_path, name
):
    client, store, fixture = _context(monkeypatch, vault, tmp_path, name, semantic="failing")
    before = store.snapshot()
    context = _ask(client, fixture).json()["context"]
    assert context["reduced"] is True
    assert context["sources"]["semantic"] == "unavailable"
    assert "semantic" not in context["score_basis"]
    assert all(item["components"]["semantic"] is None for item in context["items"])
    assert fixture["cold_incident"] in _ids(context)
    assert fixture["hot_arc"] not in _ids(context)
    assert "embeddings unreachable" not in json.dumps(context)
    assert store.snapshot() == before
    assert _rank().context_relevance_at_k(context["items"], fixture["relevant"], 3) == 1.0


def test_a_retry_replays_the_same_ranking_without_a_second_effect(monkeypatch, vault, tmp_path):
    client, store, fixture = _context(monkeypatch, vault, tmp_path, "first")
    first = _ask(client, fixture).json()
    again = _ask(client, fixture).json()
    assert again["replayed"] is True
    assert again["operation_id"] == first["operation_id"]
    assert again["context"] == first["context"]


def test_a_missing_graph_is_labelled_rather_than_scored(monkeypatch, vault, tmp_path):
    client = _client(monkeypatch, vault, None, TOKENS)
    context = _post(client, _request(**FIXTURE["first"]["request"])).json()["context"]
    assert context["items"] == []
    assert context["sources"] == {"graph": "not_configured"}
    assert context["score_basis"] == []


def test_a_graph_timeout_is_a_labelled_reduced_result(monkeypatch, vault, tmp_path):
    client, _, fixture = _context(monkeypatch, vault, tmp_path, "first")
    monkeypatch.setattr(importlib.import_module("app.graph").GraphReadView, "entities", _timeout)
    context = _ask(client, fixture).json()["context"]
    assert context["reduced"] is True
    assert context["sources"] == {"graph": "unavailable"}
    assert context["items"] == []


def _timeout(*_args):
    raise TimeoutError


def _locked(*_args):
    raise sqlite3.OperationalError("database is locked")


def test_restoring_the_previous_policy_revision_keeps_the_source_store(
    monkeypatch, vault, tmp_path
):
    client, store, fixture = _context(monkeypatch, vault, tmp_path, "first")
    current = _ask(client, fixture).json()
    before = store.snapshot()
    monkeypatch.setenv("BRAIN_RANKING_POLICY", "1")
    restored = _ask(client, fixture).json()
    assert restored["replayed"] is False
    assert restored["operation_id"] != current["operation_id"]
    assert restored["context"]["policy_revision"] == "1"
    assert all(item["components"]["semantic"] is not None for item in restored["context"]["items"])
    assert fixture["cold_incident"] in _ids(restored["context"])
    assert fixture["hot_arc"] not in _ids(restored["context"])
    assert store.snapshot() == before


def test_an_unknown_policy_revision_is_refused(monkeypatch, vault, tmp_path):
    client, store, fixture = _context(
        monkeypatch, vault, tmp_path, "first", BRAIN_RANKING_POLICY="9"
    )
    before = store.snapshot()
    response = _ask(client, fixture)
    assert response.status_code == 503
    assert store.snapshot() == before
    monkeypatch.setenv("BRAIN_RANKING_POLICY", "2")
    assert _ask(client, fixture).json()["context"]["policy_revision"] == "2"
    assert "BRAIN_RANKING_POLICY" in response.text


def test_disabling_ranking_restores_the_accepted_request_response(monkeypatch, vault, tmp_path):
    client, _, fixture = _context(monkeypatch, vault, tmp_path, "first")
    monkeypatch.setenv("BRAIN_CONTEXT_RANKING", "false")
    plain = _ask(client, fixture).json()
    assert "context" not in plain
    ctx = importlib.import_module("app.context")
    request = ctx.ContextRequest.model_validate(_request(**fixture["request"]))
    assert plain["operation_id"] == ctx.operation_id(request, plain["scope"])


def test_relevance_at_k_counts_relevant_items_in_the_top_k():
    rank = _rank()
    items = [{"id": "a"}, {"id": "x"}, {"id": "b"}]
    assert rank.context_relevance_at_k(items, ["a", "b"], 2) == 0.5
    assert rank.context_relevance_at_k(items, ["a", "b"], 3) == 1.0
    assert rank.context_relevance_at_k(items, [], 3) == 0.0
    assert rank.context_relevance_at_k(items, ["a"], 0) == 0.0


def test_a_malformed_optional_source_answer_is_labelled_unavailable(monkeypatch, vault, tmp_path):
    client, _, fixture = _context(monkeypatch, vault, tmp_path, "first")
    ctx = importlib.import_module("app.context")
    monkeypatch.setattr(ctx, "heat_source", lambda _query, ids: dict.fromkeys(ids, "hot"))
    context = _ask(client, fixture).json()["context"]
    assert context["sources"]["heat"] == "unavailable"
    assert context["reduced"] is True
    assert "heat" not in context["score_basis"]
    assert all(item["components"]["heat"] is None for item in context["items"])


def test_a_graph_store_error_is_a_labelled_reduced_result(monkeypatch, vault, tmp_path):
    client, _, fixture = _context(monkeypatch, vault, tmp_path, "first")
    monkeypatch.setattr(importlib.import_module("app.graph").GraphReadView, "entities", _locked)
    context = _ask(client, fixture).json()["context"]
    assert context["sources"] == {"graph": "unavailable"}
    assert context["reduced"] is True
    assert "locked" not in json.dumps(context)


def test_a_partial_edge_read_marks_the_result_reduced(monkeypatch, vault, tmp_path):
    client, _, fixture = _context(monkeypatch, vault, tmp_path, "first")
    monkeypatch.setattr(_rank(), "EDGE_BUDGET", 1)
    context = _ask(client, fixture).json()["context"]
    assert context["sources"]["graph"] == "partial"
    assert context["reduced"] is True


@pytest.mark.parametrize("name", SETS)
def test_a_restarted_process_ranks_the_same_request_identically(monkeypatch, vault, tmp_path, name):
    client, store, fixture = _context(monkeypatch, vault, tmp_path, name)
    first = _ask(client, fixture).json()
    before = store.snapshot()
    restarted = _client(monkeypatch, vault, tmp_path / f"{name}.db", TOKENS)
    _sources(monkeypatch, fixture, "fixture")
    after = _ask(restarted, fixture).json()
    assert after["replayed"] is False
    assert after["operation_id"] == first["operation_id"]
    assert after["context"] == first["context"]
    assert store.snapshot() == before
