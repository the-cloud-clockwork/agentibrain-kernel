"""SV2-MUL-05: distinct per-brain policy revisions and inference budgets."""

import asyncio
import copy
import importlib
import json
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from .test_context_request import _post, _request
from .test_graph import _MODULES, ROUTER, _auth

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "sv2_mul05_policy.json").read_text())
SETS = ["first", "second"]
POLICY_MODULES = ["app.scope", "app.brain_policy", *_MODULES[_MODULES.index("app.graph") :]]


def _policy():
    return importlib.import_module("app.brain_policy")


def _write(path: Path, document: dict) -> Path:
    path.write_text(json.dumps(document))
    return path


def _instance(monkeypatch, tmp_path, fixture, brain, revision, policy_file=None):
    """Start one brain-api instance: its own graph, policy file and token on a shared backend."""
    project = fixture["project"]
    db = tmp_path / f"{brain}.db"
    fresh = not db.exists()
    path = policy_file or _write(tmp_path / f"{brain}-policy.json", fixture[brain])
    monkeypatch.setenv("BRAIN_ID", brain)
    monkeypatch.setenv("BRAIN_GRAPH_DB", str(db))
    monkeypatch.setenv("KB_ROUTER_TOKENS", ROUTER)
    monkeypatch.setenv("KB_WORKER_TOKENS", f"{brain}@{project}#eng=agent")
    monkeypatch.setenv("BRAIN_POLICY_FILE", str(path))
    monkeypatch.setenv("BRAIN_POLICY_REVISION", revision)
    monkeypatch.setenv(
        "BRAIN_INFERENCE_CEILING_CONCURRENCY", str(fixture["ceiling"]["concurrency"])
    )
    monkeypatch.setenv(
        "BRAIN_INFERENCE_CEILING_TOKENS_PER_MINUTE", str(fixture["ceiling"]["tokens_per_minute"])
    )
    for name in POLICY_MODULES:
        importlib.reload(importlib.import_module(name))
    store = importlib.import_module("app.graph").GraphStore(db)
    if fresh:
        for entity in fixture["entities"]:
            store.put_entity(entity)
    ctx = importlib.import_module("app.context")
    semantic = fixture["semantic"]
    monkeypatch.setattr(ctx, "semantic_source", lambda _q, ids: {i: semantic[i] for i in ids})
    monkeypatch.setattr(ctx, "heat_source", None)
    from app.main import app

    return TestClient(app), path


def _ask(client, fixture, brain, n=1):
    body = _request(
        **{
            **fixture["request"],
            "brain_id": brain,
            "project_id": fixture["project"],
            "request_id": f"mul05-request-{n:04d}",
            "execution_id": f"exe-mul05-{n:04d}",
        }
    )
    return _post(client, body, "agent")


def _pack(response) -> dict:
    assert response.status_code == 200, response.text
    body = response.json()
    return {"context": body["context"], "brain_policy": body["brain_policy"]}


def _stats(client) -> dict:
    return client.get("/v2/context/stats", headers=_auth(ROUTER)).json()


@pytest.mark.parametrize("name", SETS)
def test_a_swarm_policy_change_affects_only_swarm_packs(monkeypatch, vault, tmp_path, name):
    fixture = copy.deepcopy(FIXTURE[name])
    personal_revision = next(iter(fixture["personal"]["revisions"]))
    personal_policy = fixture["personal"]["revisions"][personal_revision]

    personal, _ = _instance(monkeypatch, tmp_path, fixture, "personal", personal_revision)
    before = _pack(_ask(personal, fixture, "personal"))
    personal_usage = _stats(personal)["brain_inference_budget_usage"]

    swarm, swarm_file = _instance(monkeypatch, tmp_path, fixture, "swarm", "1")
    steady = _pack(_ask(swarm, fixture, "swarm"))
    swarm, _ = _instance(monkeypatch, tmp_path, fixture, "swarm", "2", swarm_file)
    tuned = _pack(_ask(swarm, fixture, "swarm"))

    personal, _ = _instance(monkeypatch, tmp_path, fixture, "personal", personal_revision)
    after = _pack(_ask(personal, fixture, "personal"))

    assert after == before
    assert before["brain_policy"]["brain_id"] == "personal"
    assert before["brain_policy"]["revision"] == personal_revision
    assert before["brain_policy"]["source"] == "file"
    assert before["context"]["policy_revision"] == personal_policy["ranking"]
    assert len(before["context"]["items"]) <= personal_policy["max_items"]
    assert before["context"]["pack"]["degraded"] == "packing_disabled"
    assert _stats(personal)["brain_inference_budget_usage"] == personal_usage
    assert personal_usage["brain_id"] == "personal"
    assert personal_usage["concurrency_limit"] == personal_policy["inference_concurrency"]
    assert (
        personal_usage["tokens_per_minute_limit"]
        == (personal_policy["inference_tokens_per_minute"])
    )

    experimental = fixture["swarm"]["revisions"]["2"]
    assert steady["brain_policy"]["revision"] == "1"
    assert tuned["brain_policy"]["revision"] == "2"
    assert tuned["brain_policy"]["digest"] != steady["brain_policy"]["digest"]
    assert tuned["context"]["policy_revision"] == experimental["ranking"]
    assert tuned["context"]["pack"]["degraded"] is None
    assert len(tuned["context"]["items"]) > len(steady["context"]["items"])
    assert len(steady["context"]["items"]) <= fixture["swarm"]["revisions"]["1"]["max_items"]
    instance = swarm.get("/instance").json()
    assert instance["brain_policy"] == tuned["brain_policy"]
    assert _stats(swarm)["brain_policy"]["active"] == tuned["brain_policy"]


@pytest.mark.parametrize("name", SETS)
def test_a_inference_budgets_bound_concurrency_and_tokens_separately(
    monkeypatch, vault, tmp_path, name
):
    fixture = copy.deepcopy(FIXTURE[name])
    _instance(monkeypatch, tmp_path, fixture, "swarm", "2")
    policy = _policy()
    now = [0.0]
    budget = policy.InferenceBudget(clock=lambda: now[0])
    current = policy.active()
    limits = fixture["swarm"]["revisions"]["2"]

    leases = [budget.reserve(current) for _ in range(limits["inference_concurrency"])]
    assert all(leases)
    assert budget.reserve(current) is None
    for lease in leases:
        budget.release(lease)

    per_call = limits["synthesis_max_tokens"]
    allowed = limits["inference_tokens_per_minute"] // per_call - len(leases)
    for _ in range(allowed):
        budget.release(budget.reserve(current))
    assert budget.reserve(current) is None
    usage = budget.usage(current)
    assert usage["refused_concurrency"] == 1
    assert usage["refused_tokens"] == 1
    assert usage["tokens_reserved_window"] == (len(leases) + allowed) * per_call
    assert usage["in_flight"] == 0

    now[0] += policy.WINDOW_SECONDS
    assert budget.reserve(current) is not None


@pytest.mark.parametrize("name", SETS)
def test_a_router_inference_spends_the_brain_synthesis_budget(monkeypatch, vault, tmp_path, name):
    fixture = copy.deepcopy(FIXTURE[name])
    _instance(monkeypatch, tmp_path, fixture, "swarm", "2")
    router = importlib.reload(importlib.import_module("app.router"))
    policy = _policy()
    monkeypatch.setattr(router, "INFERENCE_URL", "http://inference.fixture")
    sent = []

    class _Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": json.dumps({"route": "raw"})}}]}

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json, headers):
            sent.append(json)
            return _Response()

    monkeypatch.setattr(router.httpx, "AsyncClient", _Client)
    asyncio.run(router._call_router_llm("a lesson", "brain-classify"))
    assert sent[0]["max_tokens"] == fixture["swarm"]["revisions"]["2"]["synthesis_max_tokens"]
    usage = policy.budget.usage(policy.active())
    assert usage["granted"] == 1
    assert usage["in_flight"] == 0

    held = [policy.budget.reserve(policy.active()) for _ in range(usage["concurrency_limit"])]
    assert all(held)
    asyncio.run(router._call_router_llm("a lesson", "brain-classify"))
    assert len(sent) == 1
    assert policy.budget.usage(policy.active())["refused_concurrency"] == 1


@pytest.mark.parametrize("name", SETS)
def test_b_a_budget_above_the_ceiling_is_refused_without_raising_it(
    monkeypatch, vault, tmp_path, name
):
    fixture = copy.deepcopy(FIXTURE[name])
    personal_revision = next(iter(fixture["personal"]["revisions"]))
    personal, _ = _instance(monkeypatch, tmp_path, fixture, "personal", personal_revision)
    personal_before = _pack(_ask(personal, fixture, "personal"))
    personal_stats = _stats(personal)["brain_inference_budget_usage"]

    swarm, swarm_file = _instance(monkeypatch, tmp_path, fixture, "swarm", "3")
    protected = swarm_file.read_bytes()
    refused = _ask(swarm, fixture, "swarm")

    assert refused.status_code == 503
    detail = refused.json()["detail"]
    assert detail["reason"] == "policy_refused"
    assert detail["refusal"] == "budget_exceeds_ceiling"
    assert str(swarm_file) not in refused.text
    policy = _policy()
    assert policy.ceiling() == policy.Ceiling(**fixture["ceiling"])
    assert policy.reserve() is None
    stats = _stats(swarm)
    assert stats["brain_policy"]["active"] is None
    assert stats["brain_policy"]["refusal"]["reason"] == "budget_exceeds_ceiling"
    assert stats["brain_policy"]["ceiling"] == fixture["ceiling"]
    assert stats["brain_inference_budget_usage"] is None
    assert swarm.get("/instance").json()["brain_policy"] is None
    assert swarm_file.read_bytes() == protected

    personal, _ = _instance(monkeypatch, tmp_path, fixture, "personal", personal_revision)
    assert _pack(_ask(personal, fixture, "personal")) == personal_before
    assert _stats(personal)["brain_inference_budget_usage"] == personal_stats

    swarm, _ = _instance(monkeypatch, tmp_path, fixture, "swarm", "2", swarm_file)
    assert _pack(_ask(swarm, fixture, "swarm", 2))["brain_policy"]["revision"] == "2"


@pytest.mark.parametrize(
    ("change", "refusal"),
    [
        ({"ceiling": {"concurrency": 99, "tokens_per_minute": 999999}}, "invalid_policy"),
        ({"brain_id": "personal"}, "brain_audience"),
    ],
)
def test_b_a_policy_cannot_carry_a_ceiling_or_another_brain(
    monkeypatch, vault, tmp_path, change, refusal
):
    fixture = copy.deepcopy(FIXTURE["first"])
    path = _write(tmp_path / "swarm-policy.json", {**fixture["swarm"], **change})
    swarm, _ = _instance(monkeypatch, tmp_path, fixture, "swarm", "2", path)

    response = _ask(swarm, fixture, "swarm")

    assert response.status_code == 503
    assert response.json()["detail"]["refusal"] == refusal
    assert _policy().ceiling() == _policy().Ceiling(**fixture["ceiling"])


@pytest.mark.parametrize("name", SETS)
def test_b_a_policy_without_a_deployment_ceiling_is_refused(monkeypatch, vault, tmp_path, name):
    fixture = copy.deepcopy(FIXTURE[name])
    swarm, _ = _instance(monkeypatch, tmp_path, fixture, "swarm", "1")
    monkeypatch.delenv("BRAIN_INFERENCE_CEILING_TOKENS_PER_MINUTE")

    response = _ask(swarm, fixture, "swarm")

    assert response.status_code == 503
    assert response.json()["detail"]["refusal"] == "ceiling_unset"


def test_without_a_policy_file_the_environment_behavior_is_kept(monkeypatch, vault, tmp_path):
    fixture = copy.deepcopy(FIXTURE["first"])
    swarm, _ = _instance(monkeypatch, tmp_path, fixture, "swarm", "1")
    monkeypatch.delenv("BRAIN_POLICY_FILE")
    monkeypatch.delenv("BRAIN_INFERENCE_CEILING_CONCURRENCY")
    monkeypatch.delenv("BRAIN_INFERENCE_CEILING_TOKENS_PER_MINUTE")
    monkeypatch.setenv("BRAIN_RANKING_POLICY", "1")
    monkeypatch.setenv("BRAIN_CONTEXT_PACKING", "false")

    pack = _pack(_ask(swarm, fixture, "swarm"))
    policy = _policy().active()

    assert pack["brain_policy"]["source"] == "environment"
    assert pack["context"]["policy_revision"] == "1"
    assert pack["context"]["pack"]["degraded"] == "packing_disabled"
    assert len(pack["context"]["items"]) == len(fixture["entities"])
    assert policy.policy.synthesis_max_tokens == 2048
    leases = [_policy().budget.reserve(policy) for _ in range(64)]
    assert all(leases)


@pytest.mark.parametrize("name", SETS)
def test_c_reverting_a_revision_invalidates_only_that_instance_cache(
    monkeypatch, vault, tmp_path, name
):
    fixture = copy.deepcopy(FIXTURE[name])
    personal_file = _write(tmp_path / "personal-policy.json", fixture["personal"])
    swarm, swarm_file = _instance(monkeypatch, tmp_path, fixture, "swarm", "1")
    steady = _pack(_ask(swarm, fixture, "swarm"))
    swarm, _ = _instance(monkeypatch, tmp_path, fixture, "swarm", "2", swarm_file)
    tuned = _pack(_ask(swarm, fixture, "swarm"))
    assert _pack(_ask(swarm, fixture, "swarm"))["context"] == tuned["context"]
    assert _stats(swarm)["context_cache"]["context_cache_hits"] == 1

    other = copy.deepcopy(fixture["personal"])
    next(iter(other["revisions"].values()))["max_items"] = 50
    _write(personal_file, other)
    _ask(swarm, fixture, "swarm")
    cache = _stats(swarm)["context_cache"]
    assert cache["context_cache_invalidations"] == 0
    assert cache["context_cache_hits"] == 2

    reverted = copy.deepcopy(fixture["swarm"])
    reverted["revisions"]["2"] = reverted["revisions"]["1"]
    _write(swarm_file, reverted)
    restored = _pack(_ask(swarm, fixture, "swarm"))
    cache = _stats(swarm)["context_cache"]
    assert cache["context_cache_invalidations"] == 1
    assert cache["context_cache_misses"] == 2
    assert restored["context"] == steady["context"]

    swarm, _ = _instance(monkeypatch, tmp_path, fixture, "swarm", "1", swarm_file)
    first = _ask(swarm, fixture, "swarm", 7).json()
    replay = _ask(swarm, fixture, "swarm", 7).json()
    assert replay["replayed"] is True
    assert replay["operation_id"] == first["operation_id"]
    assert first["context"] == steady["context"]


@pytest.mark.parametrize("name", SETS)
def test_c_a_load_begun_under_an_older_revision_is_not_stored(monkeypatch, vault, tmp_path, name):
    fixture = copy.deepcopy(FIXTURE[name])
    swarm, swarm_file = _instance(monkeypatch, tmp_path, fixture, "swarm", "2")
    rank = importlib.import_module("app.retrieval.rank")
    real = rank.retrieve
    entered, release = threading.Event(), threading.Event()

    def held(*args, **kwargs):
        entered.set()
        release.wait(10)
        return real(*args, **kwargs)

    monkeypatch.setattr(rank, "retrieve", held)
    worker = threading.Thread(target=lambda: _ask(swarm, fixture, "swarm"))
    worker.start()
    assert entered.wait(10)
    reverted = copy.deepcopy(fixture["swarm"])
    reverted["revisions"]["2"] = reverted["revisions"]["1"]
    _write(swarm_file, reverted)
    monkeypatch.setattr(rank, "retrieve", real)
    current = _pack(_ask(swarm, fixture, "swarm", 2))
    release.set()
    worker.join(10)

    assert current["brain_policy"]["digest"] == _policy().active().digest
    assert _stats(swarm)["context_cache"]["context_cache_entries"] == 1
    again = _pack(_ask(swarm, fixture, "swarm", 3))
    assert again["context"] == current["context"]
