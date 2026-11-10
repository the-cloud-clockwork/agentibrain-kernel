"""Tick synthesis is charged to the inference budget of the brain it runs for."""

import copy
import importlib
import json

import httpx
import pytest

from .test_brain_policy import FIXTURE, SETS, _instance, _policy, _port, _remote_stats, _serve
from .test_graph import ROUTER, _auth

PATH = "/v2/inference/synthesis"


def _body(brain: str, max_tokens: int = 100000) -> dict:
    return {"brain_id": brain, "model": "brain-brief", "prompt": "tick", "max_tokens": max_tokens}


def _gateway(monkeypatch, sent: list) -> None:
    synthesis = importlib.import_module("app.synthesis")
    monkeypatch.setenv("INFERENCE_URL", "http://inference.fixture")

    class _Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"choices": [{"message": {"content": "brief"}}]}

    class _Client:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def __aenter__(self) -> "_Client":
            return self

        async def __aexit__(self, *exc: object) -> bool:
            return False

        async def post(self, url: str, json: dict, headers: dict) -> _Response:
            sent.append(json)
            return _Response()

    monkeypatch.setattr(synthesis.httpx, "AsyncClient", _Client)


@pytest.mark.parametrize("name", SETS)
def test_tick_synthesis_spends_this_brain_budget(monkeypatch, vault, tmp_path, name):
    fixture = copy.deepcopy(FIXTURE[name])
    client, _ = _instance(monkeypatch, tmp_path, fixture, "swarm", "2")
    sent: list = []
    _gateway(monkeypatch, sent)
    policy = _policy()

    response = client.post(PATH, json=_body("swarm"), headers=_auth(ROUTER))

    assert response.status_code == 200, response.text
    assert response.json()["brain_id"] == "swarm"
    assert response.json()["content"] == "brief"
    assert sent[0]["max_tokens"] == fixture["swarm"]["revisions"]["2"]["synthesis_max_tokens"]
    usage = policy.budget.usage(policy.active())
    assert usage["granted"] == 1
    assert usage["in_flight"] == 0


@pytest.mark.parametrize("name", SETS)
def test_a_spent_budget_defers_tick_synthesis(monkeypatch, vault, tmp_path, name):
    fixture = copy.deepcopy(FIXTURE[name])
    client, _ = _instance(monkeypatch, tmp_path, fixture, "swarm", "2")
    sent: list = []
    _gateway(monkeypatch, sent)
    policy = _policy()
    limit = fixture["swarm"]["revisions"]["2"]["inference_concurrency"]
    held = [policy.budget.reserve(policy.active()) for _ in range(limit)]
    assert all(held)

    response = client.post(PATH, json=_body("swarm"), headers=_auth(ROUTER))

    assert response.status_code == 429
    assert response.json()["detail"] == "inference_budget_spent"
    assert sent == []
    assert policy.budget.usage(policy.active())["refused_concurrency"] == 1


@pytest.mark.parametrize("name", SETS)
def test_tick_synthesis_for_another_brain_is_refused(monkeypatch, vault, tmp_path, name):
    fixture = copy.deepcopy(FIXTURE[name])
    client, _ = _instance(monkeypatch, tmp_path, fixture, "swarm", "2")
    sent: list = []
    _gateway(monkeypatch, sent)
    policy = _policy()

    response = client.post(PATH, json=_body("personal"), headers=_auth(ROUTER))

    assert response.status_code == 403
    assert sent == []
    assert policy.budget.usage(policy.active())["granted"] == 0


@pytest.mark.parametrize("name", SETS)
def test_tick_synthesis_needs_a_router_token(monkeypatch, vault, tmp_path, name):
    fixture = copy.deepcopy(FIXTURE[name])
    client, _ = _instance(monkeypatch, tmp_path, fixture, "swarm", "2")
    sent: list = []
    _gateway(monkeypatch, sent)

    response = client.post(PATH, json=_body("swarm"), headers=_auth("agent"))

    assert response.status_code == 403
    assert sent == []


@pytest.mark.parametrize("name", SETS)
def test_a_spent_swarm_tick_budget_leaves_the_personal_budget(monkeypatch, tmp_path, name):
    fixture = copy.deepcopy(FIXTURE[name])
    personal_revision = next(iter(fixture["personal"]["revisions"]))
    monkeypatch.setenv("INFERENCE_URL", f"http://127.0.0.1:{_port()}")
    personal_file = tmp_path / "personal-policy.json"
    personal_file.write_text(json.dumps(fixture["personal"]))
    swarm_file = tmp_path / "swarm-policy.json"
    swarm_file.write_text(json.dumps(fixture["swarm"]))
    personal, personal_url = _serve(tmp_path, fixture, "personal", personal_revision, personal_file)
    swarm, swarm_url = _serve(tmp_path, fixture, "swarm", "1", swarm_file)
    try:
        before = _remote_stats(personal_url)["brain_inference_budget_usage"]
        limits = fixture["swarm"]["revisions"]["1"]
        calls = limits["inference_tokens_per_minute"] // limits["synthesis_max_tokens"]
        codes = [
            httpx.post(f"{swarm_url}{PATH}", json=_body("swarm"), headers=_auth(ROUTER)).status_code
            for _ in range(calls + 1)
        ]
        swarm_usage = _remote_stats(swarm_url)["brain_inference_budget_usage"]
        after = _remote_stats(personal_url)["brain_inference_budget_usage"]
    finally:
        personal.terminate()
        swarm.terminate()
        personal.wait(10)
        swarm.wait(10)

    assert 429 not in codes[:calls]
    assert codes[-1] == 429
    assert swarm_usage["brain_id"] == "swarm"
    assert swarm_usage["granted"] == calls
    assert swarm_usage["refused_tokens"] == 1
    assert after == before
    assert after["granted"] == 0
    assert after["tokens_reserved_window"] == 0
