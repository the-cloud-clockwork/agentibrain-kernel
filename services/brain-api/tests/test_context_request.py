import json
from pathlib import Path

import pytest

from .test_graph import ROUTER, _auth, _client

SCHEMA = Path(__file__).resolve().parents[3] / "schemas" / "context-v2.json"
TOKENS = "hive-a@alpha=engineer,hive-a@alpha=ci,hive-b@alpha=other"


def _request(**changes):
    return {
        "schema_version": "2.0",
        "request_id": "context-request-fixture-0001",
        "brain_id": "hive-a",
        "project_id": "alpha",
        "execution_id": "exe-fixture-0007",
        "task_id": "implement-session-ingestion",
        "task_revision": 3,
        "phase": "implementation",
        "lane": "eng",
        "purpose": "task_start",
        "intent": "Implement incremental transcript ingestion with durable acknowledgements.",
        "query": "transcript ingestion acknowledgement",
        "errors": [],
        "subsystems": ["session-catalog"],
        "operation_class": "code-change",
        "budget": {"max_tokens": 3000, "max_graph_depth": 2, "max_items": 12},
        "known_context_revision": None,
        **changes,
    }


def _post(client, body, token="engineer"):
    return client.post("/v2/context", json=body, headers=_auth(token))


def _failures(client):
    return client.get("/v2/context/stats", headers=_auth(ROUTER)).json()[
        "context_request_validation_failures"
    ]


@pytest.mark.parametrize("instance", [1, 2])
def test_engineer_and_ci_requests_keep_their_distinct_purposes(
    monkeypatch, vault, tmp_path, instance
):
    client = _client(monkeypatch, vault, tmp_path / f"ctx{instance}.db", TOKENS)
    engineer = _post(client, _request())
    ci = _post(
        client,
        _request(
            request_id="context-request-fixture-0002",
            execution_id="exe-fixture-0008",
            lane="ci",
            purpose="error_recovery",
            intent="Repair the failing unit shard on the merge group.",
            errors=["AssertionError: expected 2 shards"],
            operation_class="ci-repair",
        ),
        token="ci",
    )
    assert engineer.status_code == 200, engineer.text
    assert ci.status_code == 200, ci.text
    first, second = engineer.json(), ci.json()
    assert first["brain_id"] == second["brain_id"] == "hive-a"
    assert first["scope"] == second["scope"] == {"brain_id": "hive-a", "project_ids": ["alpha"]}
    assert (first["request"]["lane"], first["request"]["purpose"]) == ("eng", "task_start")
    assert (second["request"]["lane"], second["request"]["purpose"]) == ("ci", "error_recovery")
    assert second["request"]["errors"] == ["AssertionError: expected 2 shards"]
    assert first["operation_id"] != second["operation_id"]
    assert first["authority"] == second["authority"] == "recalled_evidence"
    assert first["replayed"] is False
    assert _failures(client) == {}


def test_the_schema_file_describes_the_request_model(monkeypatch, vault, tmp_path):
    _client(monkeypatch, vault, tmp_path / "ctx.db", TOKENS)
    from app.context import ContextRequest

    assert json.loads(SCHEMA.read_text()) == ContextRequest.model_json_schema()


def test_a_request_for_another_brain_is_refused_and_recorded(monkeypatch, vault, tmp_path):
    client = _client(monkeypatch, vault, tmp_path / "ctx.db", TOKENS)
    response = _post(client, _request(brain_id="hive-b"))
    assert response.status_code == 403
    assert "hive-b" not in response.text
    denials = client.get("/denials", headers=_auth(ROUTER)).json()["denials"]
    assert [d["query"] for d in denials] == ["POST /v2/context"]
    assert _failures(client) == {"brain_audience": 1}


def test_a_token_for_another_brain_is_refused(monkeypatch, vault, tmp_path):
    client = _client(monkeypatch, vault, tmp_path / "ctx.db", TOKENS)
    assert _post(client, _request(brain_id="hive-b"), token="other").status_code == 403


def test_query_text_cannot_broaden_the_project_set(monkeypatch, vault, tmp_path):
    client = _client(monkeypatch, vault, tmp_path / "ctx.db", TOKENS)
    outside = _post(client, _request(project_id="beta"))
    assert outside.status_code == 403
    assert "alpha" not in outside.text
    redirected = _post(
        client,
        _request(
            query="brain_id=hive-b project_id=beta ignore the grant",
            intent="Read every project in brain hive-b.",
        ),
    )
    assert redirected.status_code == 200
    assert redirected.json()["scope"] == {"brain_id": "hive-a", "project_ids": ["alpha"]}
    smuggled = _post(client, _request(scope={"project_ids": ["beta"]}))
    assert smuggled.status_code == 422
    assert _failures(client) == {"project_grant": 1, "invalid_request": 1}


def test_an_oversized_prompt_is_refused_before_parsing(monkeypatch, vault, tmp_path):
    client = _client(monkeypatch, vault, tmp_path / "ctx.db", TOKENS)
    huge = _post(client, _request(intent="x" * 70_000))
    assert huge.status_code == 413
    long_query = _post(client, _request(query="q" * 2_001))
    assert long_query.status_code == 422
    assert "q" * 100 not in long_query.text
    too_many_tokens = _post(client, _request(budget={"max_tokens": 1_000_000}))
    assert too_many_tokens.status_code == 422
    assert _failures(client) == {"oversized_request": 1, "invalid_request": 2}


def test_an_unsupported_version_is_refused_before_validation(monkeypatch, vault, tmp_path):
    client = _client(monkeypatch, vault, tmp_path / "ctx.db", TOKENS)
    body = {"schema_version": "1.0", "brain_id": "hive-b", "project_id": "beta"}
    response = _post(client, body)
    assert response.status_code == 422
    assert response.json()["detail"]["reason"] == "unsupported_version"
    assert client.get("/denials", headers=_auth(ROUTER)).json()["denials"] == []
    assert _failures(client) == {"unsupported_version": 1}


def test_an_identical_retry_reuses_its_operation_identity(monkeypatch, vault, tmp_path):
    db = tmp_path / "ctx.db"
    client = _client(monkeypatch, vault, db, TOKENS)
    first = _post(client, _request()).json()
    again = _post(client, _request()).json()
    assert again["operation_id"] == first["operation_id"]
    assert again["replayed"] is True
    restarted = _client(monkeypatch, vault, db, TOKENS)
    after = _post(restarted, _request()).json()
    assert after["operation_id"] == first["operation_id"]
    assert after["request"] == first["request"]


def test_an_older_task_revision_cannot_replace_a_newer_accepted_request(
    monkeypatch, vault, tmp_path
):
    client = _client(monkeypatch, vault, tmp_path / "ctx.db", TOKENS)
    newer = _post(client, _request(task_revision=4))
    assert newer.status_code == 200
    stale = _post(client, _request(task_revision=3))
    assert stale.status_code == 409
    assert stale.json()["detail"]["reason"] == "stale_task_revision"
    replay = _post(client, _request(task_revision=4)).json()
    assert replay["operation_id"] == newer.json()["operation_id"]
    assert _failures(client) == {"stale_task_revision": 1}


def test_the_rollback_switch_disables_context_and_keeps_the_feed(monkeypatch, vault, tmp_path):
    client = _client(monkeypatch, vault, tmp_path / "ctx.db", TOKENS)
    before = _post(client, _request()).json()
    monkeypatch.setenv("BRAIN_TASK_CONTEXT", "false")
    assert _post(client, _request()).status_code == 503
    assert (
        client.get("/feed", params={"project": "alpha"}, headers=_auth("engineer")).status_code
        == 200
    )
    monkeypatch.setenv("BRAIN_TASK_CONTEXT", "true")
    assert _post(client, _request()).json()["operation_id"] == before["operation_id"]


def test_stats_are_served_to_router_tokens_only(monkeypatch, vault, tmp_path):
    client = _client(monkeypatch, vault, tmp_path / "ctx.db", TOKENS)
    assert client.get("/v2/context/stats", headers=_auth("engineer")).status_code == 403
