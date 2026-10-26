"""SV2-CTX-04: coalesced context requests and scoped cache invalidation."""

import copy
import dataclasses
import importlib
import json
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from .test_context_request import _post, _request
from .test_graph import _MODULES, ROUTER, _auth, _client

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "sv2_ctx04_cache.json").read_text())
SETS = ["first", "second"]


def _tokens(fixture: dict) -> str:
    a, b, empty = fixture["projects"]
    return f"hive-a@{a}|{b}|{empty}#eng=burst,hive-a@{b}#eng=solo,hive-b@{a}|{b}=other"


def _cache_module():
    return importlib.import_module("app.context_cache")


def _loaded(db: Path, fixture: dict):
    store = importlib.import_module("app.graph").GraphStore(db)
    for entity in fixture["entities"]:
        store.put_entity(entity)
    return store


def _context(monkeypatch, vault, tmp_path, name, **env):
    fixture = copy.deepcopy(FIXTURE[name])
    db = tmp_path / f"{name}.db"
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    client = _client(monkeypatch, vault, db, _tokens(fixture))
    store = _loaded(db, fixture)
    return client, store, fixture, _sources(monkeypatch)


def _sources(monkeypatch, semantic=None):
    ctx = importlib.import_module("app.context")
    monkeypatch.setattr(ctx, "semantic_source", semantic or (lambda _q, ids: {i: 0.0 for i in ids}))
    monkeypatch.setattr(ctx, "heat_source", None)
    return ctx


def _brain(monkeypatch, brain: str, db: Path):
    monkeypatch.setenv("BRAIN_ID", brain)
    monkeypatch.setenv("BRAIN_GRAPH_DB", str(db))
    for name in _MODULES:
        importlib.reload(importlib.import_module(name))
    from app.main import app

    return TestClient(app), _sources(monkeypatch)


def _counting(monkeypatch, hold=None):
    rank = importlib.import_module("app.retrieval.rank")
    real = rank.retrieve
    calls: Counter[str] = Counter()

    def retrieve(store, project, *args, **kwargs):
        calls[project] += 1
        result = real(store, project, *args, **kwargs)
        if hold is not None:
            hold(project)
        return result

    monkeypatch.setattr(rank, "retrieve", retrieve)
    return calls


def _ask(client, fixture, project, n, token="burst", **changes):
    body = _request(
        **{
            **fixture["request"],
            "project_id": project,
            "request_id": f"ctx04-request-{n:04d}",
            "execution_id": f"exe-burst-{n:04d}",
            **changes,
        }
    )
    return _post(client, body, token)


def _ids(fixture: dict, project: str) -> set[str]:
    return {e["id"] for e in fixture["entities"] if project in e["projects"]}


def _stats(client) -> dict:
    return client.get("/v2/context/stats", headers=_auth(ROUTER)).json()["context_cache"]


def _wait(predicate, seconds: float = 10.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


@pytest.mark.parametrize("name", SETS)
def test_a_a_launch_burst_shares_one_retrieval_while_another_project_stays_independent(
    monkeypatch, vault, tmp_path, name
):
    client, store, fixture, ctx = _context(monkeypatch, vault, tmp_path, name)
    first, second, _ = fixture["projects"]
    burst = fixture["burst"]
    entered, other_done = threading.Event(), threading.Event()

    def hold(project):
        if project != first:
            return
        entered.set()
        assert _wait(
            lambda: (
                ctx.cache.stats()["context_coalesced_requests"] == burst - 1 and other_done.is_set()
            )
        )

    monkeypatch.setattr(_cache_module(), "FOLLOWER_WAIT_SECONDS", 60.0)
    calls = _counting(monkeypatch, hold)
    before = store.snapshot()
    with ThreadPoolExecutor(burst) as pool:
        futures = [pool.submit(_ask, client, fixture, first, n) for n in range(burst)]
        assert entered.wait(10)
        other = _ask(client, fixture, second, burst)
        other_done.set()
        responses = [future.result() for future in futures]

    assert all(r.status_code == 200 for r in responses), [r.text for r in responses]
    bodies = [r.json() for r in responses]
    assert Counter(b["cache"]["state"] for b in bodies) == {"miss": 1, "coalesced": burst - 1}
    assert len({b["operation_id"] for b in bodies}) == burst
    contexts = [b["context"] for b in bodies]
    assert all(c == contexts[0] for c in contexts)
    assert contexts[0]["items"]
    assert {item["id"] for item in contexts[0]["items"]} <= _ids(fixture, first)
    assert all(b["scope"]["project_ids"] == [first] for b in bodies)
    assert all(b["authority"] == "recalled_evidence" for b in bodies)
    assert other.status_code == 200 and other.json()["cache"]["state"] == "miss"
    assert {item["id"] for item in other.json()["context"]["items"]} <= _ids(fixture, second)
    assert calls == {first: 1, second: 1}
    stats = _stats(client)
    assert stats["context_coalesced_requests"] == burst - 1
    assert stats["context_cache_requests"] == burst + 1
    assert stats["context_cache_hit_ratio"] == pytest.approx((burst - 1) / (burst + 1))

    later = _ask(client, fixture, first, burst + 1).json()
    assert later["cache"] == {
        "state": "hit",
        "negative": False,
        "ttl_seconds": _cache_module().TTL_SECONDS,
    }
    assert later["context"] == contexts[0]
    assert calls == {first: 1, second: 1}
    assert store.snapshot() == before


def test_a_every_key_field_separates_cached_results():
    cache_module = _cache_module()
    names = {f.name for f in dataclasses.fields(cache_module.Key)}
    assert {
        "brain_id",
        "project_id",
        "task_id",
        "task_revision",
        "lane",
        "purpose",
        "retrieval",
        "policy",
        "grant",
        "knowledge",
    } == names
    cache = cache_module.ContextCache()
    base = cache_module.Key(
        "hive-a", "org-a/ingest", "task", 3, "eng", "task_start", "r", "p", "g", "k"
    )
    loads = []

    def load():
        loads.append(1)
        return {"items": [{"id": "lesson:x"}], "pack": {"omitted": []}, "reduced": False}

    assert cache.get(base, lambda: "epoch", load)[1]["state"] == "miss"
    assert cache.get(base, lambda: "epoch", load)[1]["state"] == "hit"
    for field in sorted(names):
        value = getattr(base, field)
        changed = dataclasses.replace(
            base, **{field: value + 1 if isinstance(value, int) else value + "-other"}
        )
        assert cache.get(changed, lambda: "epoch", load)[1]["state"] == "miss", field
    assert len(loads) == len(names) + 1


@pytest.mark.parametrize("name", SETS)
def test_b_an_equal_query_in_a_project_with_the_same_basename_is_retrieved_separately(
    monkeypatch, vault, tmp_path, name
):
    client, store, fixture, ctx = _context(monkeypatch, vault, tmp_path, name)
    first, second, _ = fixture["projects"]
    assert first.rsplit("/", 1)[-1] == second.rsplit("/", 1)[-1]
    calls = _counting(monkeypatch)
    one = _ask(client, fixture, first, 1).json()
    two = _ask(client, fixture, second, 2).json()
    assert one["cache"]["state"] == two["cache"]["state"] == "miss"
    assert calls == {first: 1, second: 1}
    assert {i["id"] for i in one["context"]["items"]} <= _ids(fixture, first)
    assert {i["id"] for i in two["context"]["items"]} <= _ids(fixture, second)
    assert ctx.cache_key(
        ctx.ContextRequest.model_validate(_request(**fixture["request"], project_id=first)),
        {"brain_id": "hive-a", "project_ids": [first]},
        frozenset(),
        frozenset(),
        {},
        "k",
    ) != ctx.cache_key(
        ctx.ContextRequest.model_validate(_request(**fixture["request"], project_id=first)),
        {"brain_id": "hive-b", "project_ids": [first]},
        frozenset(),
        frozenset(),
        {},
        "k",
    )


@pytest.mark.parametrize("name", SETS)
def test_b_an_equal_query_to_two_brains_returns_each_brain_its_own_context(
    monkeypatch, vault, tmp_path, name
):
    client, _, fixture, _ = _context(monkeypatch, vault, tmp_path, name)
    first = fixture["projects"][0]
    a = _ask(client, fixture, first, 1).json()
    other = copy.deepcopy(fixture)
    for entity in other["entities"]:
        entity["title"] = f"{entity['title']} in brain b"
    _loaded(tmp_path / f"{name}-b.db", other)
    client_b, _ = _brain(monkeypatch, "hive-b", tmp_path / f"{name}-b.db")
    calls = _counting(monkeypatch)
    b = _ask(client_b, fixture, first, 1, token="other", brain_id="hive-b")
    assert b.status_code == 200, b.text
    assert b.json()["cache"]["state"] == "miss"
    assert b.json()["scope"] == {"brain_id": "hive-b", "project_ids": [first]}
    titles_b = [i["title"] for i in b.json()["context"]["items"]]
    assert titles_b and all(t.endswith(" in brain b") for t in titles_b)
    assert not any(i["title"].endswith(" in brain b") for i in a["context"]["items"])
    again = _ask(client_b, fixture, first, 2, token="other", brain_id="hive-b").json()
    assert again["cache"]["state"] == "hit" and again["context"] == b.json()["context"]
    assert calls == {first: 1}


@pytest.mark.parametrize("name", SETS)
def test_b_another_brain_and_an_ungranted_project_are_refused_before_any_cached_read(
    monkeypatch, vault, tmp_path, name
):
    client, store, fixture, ctx = _context(monkeypatch, vault, tmp_path, name)
    first, second, _ = fixture["projects"]
    calls = _counting(monkeypatch)
    cached = _ask(client, fixture, first, 1).json()
    titles = [i["title"] for i in cached["context"]["items"]]
    before = (store.snapshot(), copy.deepcopy(dict(ctx._accepted)), _stats(client))

    refused = [
        _ask(client, fixture, first, 2, token="other"),
        _ask(client, fixture, first, 3, brain_id="hive-b"),
        _ask(client, fixture, first, 4, token="solo"),
    ]
    assert [r.status_code for r in refused] == [403, 403, 403]
    assert [r.json()["detail"]["reason"] for r in refused[1:]] == [
        "brain_audience",
        "project_grant",
    ]
    for response in refused:
        assert all(title not in response.text for title in titles)
    assert (store.snapshot(), dict(ctx._accepted), _stats(client)) == before
    assert calls == {first: 1}

    corrected = _ask(client, fixture, second, 5, token="solo").json()
    assert corrected["cache"]["state"] == "miss"
    burst_grant = _ask(client, fixture, second, 6).json()
    assert burst_grant["cache"]["state"] == "miss"
    assert calls == {first: 1, second: 2}


@pytest.mark.parametrize("name", SETS)
def test_c_a_revoked_grant_invalidates_cached_reads_before_another_response(
    monkeypatch, vault, tmp_path, name
):
    client, store, fixture, ctx = _context(monkeypatch, vault, tmp_path, name)
    scope = importlib.import_module("app.scope")
    first, second, empty = fixture["projects"]
    calls = _counting(monkeypatch)
    _ask(client, fixture, first, 1)
    _ask(client, fixture, second, 2)
    assert _stats(client)["context_cache_entries"] == 2
    granted = scope.PROJECT_GRANTS["burst"]

    monkeypatch.setitem(scope.PROJECT_GRANTS, "burst", frozenset({second, empty}))
    refused = _ask(client, fixture, first, 3)
    assert refused.status_code == 403
    stats = _stats(client)
    assert stats["context_cache_entries"] == 0
    assert stats["context_cache_invalidations"] == 1
    after = _ask(client, fixture, second, 4).json()
    assert after["cache"]["state"] == "miss"

    monkeypatch.setitem(scope.PROJECT_GRANTS, "burst", granted)
    regranted = _ask(client, fixture, first, 5).json()
    assert regranted["cache"]["state"] == "miss"
    assert calls == {first: 2, second: 2}


@pytest.mark.parametrize("name", SETS)
def test_c_an_updated_decision_revision_is_served_instead_of_the_cached_one(
    monkeypatch, vault, tmp_path, name
):
    client, store, fixture, ctx = _context(monkeypatch, vault, tmp_path, name)
    first = fixture["projects"][0]
    update = fixture["update"]
    calls = _counting(monkeypatch)
    old = _ask(client, fixture, first, 1).json()
    assert fixture["stale_title"] in [i["title"] for i in old["context"]["items"]]

    writer = importlib.import_module("app.graph").GraphStore(tmp_path / f"{name}.db")
    assert writer.put_entity(update) == "updated"
    new = _ask(client, fixture, first, 2).json()
    assert new["cache"]["state"] == "miss"
    decision = next(i for i in new["context"]["items"] if i["id"] == update["id"])
    assert decision["revision"] == update["revision"] and decision["title"] == update["title"]
    assert fixture["stale_title"] not in json.dumps(new["context"])
    assert calls == {first: 2}
    replay = _ask(client, fixture, first, 2).json()
    assert replay["replayed"] is True and replay["context"] == new["context"]


@pytest.mark.parametrize("name", SETS)
def test_c_a_load_that_began_before_a_decision_update_is_never_stored(
    monkeypatch, vault, tmp_path, name
):
    client, store, fixture, ctx = _context(monkeypatch, vault, tmp_path, name)
    first = fixture["projects"][0]
    update = fixture["update"]
    writer = importlib.import_module("app.graph").GraphStore(tmp_path / f"{name}.db")
    written = []

    def hold(project):
        if not written:
            written.append(writer.put_entity(update))

    calls = _counting(monkeypatch, hold)
    stale = _ask(client, fixture, first, 1).json()
    assert written == ["updated"]
    assert fixture["stale_title"] in json.dumps(stale["context"])
    assert _stats(client)["context_cache_entries"] == 0
    fresh = _ask(client, fixture, first, 2).json()
    assert fresh["cache"]["state"] == "miss"
    assert update["title"] in json.dumps(fresh["context"])
    assert fixture["stale_title"] not in json.dumps(fresh["context"])
    assert calls == {first: 2}


@pytest.mark.parametrize("name", SETS)
def test_c_a_restarted_process_starts_empty_and_serves_the_same_context(
    monkeypatch, vault, tmp_path, name
):
    client, _, fixture, _ = _context(monkeypatch, vault, tmp_path, name)
    first = fixture["projects"][0]
    cached = _ask(client, fixture, first, 1).json()
    assert _ask(client, fixture, first, 2).json()["cache"]["state"] == "hit"
    restarted = _client(monkeypatch, vault, tmp_path / f"{name}.db", _tokens(fixture))
    _sources(monkeypatch)
    assert _stats(restarted)["context_cache_entries"] == 0
    after = _ask(restarted, fixture, first, 3).json()
    assert after["cache"]["state"] == "miss"
    assert after["context"] == cached["context"]


@pytest.mark.parametrize("name", SETS)
def test_c_negative_results_expire_sooner_than_positive_ones(monkeypatch, vault, tmp_path, name):
    client, store, fixture, ctx = _context(monkeypatch, vault, tmp_path, name)
    cache_module = _cache_module()
    first, _, empty = fixture["projects"]
    now = [1000.0]
    monkeypatch.setattr(ctx.cache, "clock", lambda: now[0])
    calls = _counting(monkeypatch)
    negative = _ask(client, fixture, empty, 1).json()
    assert negative["context"]["items"] == []
    assert negative["cache"] == {
        "state": "miss",
        "negative": True,
        "ttl_seconds": cache_module.NEGATIVE_TTL_SECONDS,
    }
    positive = _ask(client, fixture, first, 2).json()
    assert positive["cache"] == {
        "state": "miss",
        "negative": False,
        "ttl_seconds": cache_module.TTL_SECONDS,
    }
    assert cache_module.NEGATIVE_TTL_SECONDS < cache_module.TTL_SECONDS
    assert _ask(client, fixture, empty, 3).json()["cache"]["state"] == "hit"
    now[0] += cache_module.NEGATIVE_TTL_SECONDS + 0.5
    assert _ask(client, fixture, empty, 4).json()["cache"]["state"] == "miss"
    assert _ask(client, fixture, first, 5).json()["cache"]["state"] == "hit"
    now[0] += cache_module.TTL_SECONDS
    assert _ask(client, fixture, first, 6).json()["cache"]["state"] == "miss"
    assert calls == {empty: 2, first: 2}


@pytest.mark.parametrize("name", SETS)
def test_c_a_reduced_result_is_negative_even_with_items(monkeypatch, vault, tmp_path, name):
    client, _, fixture, _ = _context(monkeypatch, vault, tmp_path, name)
    cache_module = _cache_module()

    def failing(_query, _ids):
        raise RuntimeError("semantic source down")

    _sources(monkeypatch, failing)
    reduced = _ask(client, fixture, fixture["projects"][0], 1).json()
    assert reduced["context"]["reduced"] is True and reduced["context"]["items"]
    assert reduced["cache"] == {
        "state": "miss",
        "negative": True,
        "ttl_seconds": cache_module.NEGATIVE_TTL_SECONDS,
    }


@pytest.mark.parametrize("name", SETS)
def test_c_a_budget_that_omits_every_item_is_negative(monkeypatch, vault, tmp_path, name):
    client, _, fixture, ctx = _context(monkeypatch, vault, tmp_path, name)
    cache_module = _cache_module()
    now = [1000.0]
    monkeypatch.setattr(ctx.cache, "clock", lambda: now[0])
    first = fixture["projects"][0]
    budget = {**fixture["request"]["budget"], "max_tokens": 1}
    omitted = _ask(client, fixture, first, 1, budget=budget).json()
    assert omitted["context"]["items"] == []
    assert omitted["context"]["pack"]["omitted"]
    assert omitted["cache"] == {
        "state": "miss",
        "negative": True,
        "ttl_seconds": cache_module.NEGATIVE_TTL_SECONDS,
    }
    assert _ask(client, fixture, first, 2, budget=budget).json()["cache"]["state"] == "hit"
    now[0] += cache_module.NEGATIVE_TTL_SECONDS + 0.5
    assert _ask(client, fixture, first, 3, budget=budget).json()["cache"]["state"] == "miss"


def test_c_followers_load_alone_when_the_leader_fails_and_nothing_is_stored():
    cache_module = _cache_module()
    cache = cache_module.ContextCache()
    key = cache_module.Key(
        "hive-a", "org-a/ingest", "task", 3, "eng", "task_start", "r", "p", "g", "k"
    )
    followers = 3
    joined = threading.Event()

    def failing():
        assert _wait(lambda: cache.stats()["context_coalesced_requests"] == followers)
        joined.set()
        raise RuntimeError("transport lost")

    def load():
        return {"items": [{"id": "lesson:x"}], "pack": {"omitted": []}, "reduced": False}

    with ThreadPoolExecutor(followers + 1) as pool:
        leader = pool.submit(cache.get, key, lambda: "epoch", failing)
        assert _wait(lambda: cache.stats()["context_cache_requests"] == 1)
        waiting = [pool.submit(cache.get, key, lambda: "epoch", load) for _ in range(followers)]
        with pytest.raises(RuntimeError):
            leader.result()
        results = [w.result() for w in waiting]
    assert joined.is_set()
    assert [meta["state"] for _, meta in results] == ["bypass"] * followers
    assert cache.stats()["context_cache_entries"] == 0
    assert cache.get(key, lambda: "epoch", load)[1]["state"] == "miss"


@pytest.mark.parametrize("name", SETS)
def test_disabled_caching_keeps_bounded_per_request_retrieval(monkeypatch, vault, tmp_path, name):
    client, store, fixture, ctx = _context(
        monkeypatch, vault, tmp_path, name, BRAIN_CONTEXT_CACHE="false"
    )
    first = fixture["projects"][0]
    calls = _counting(monkeypatch)
    one = _ask(client, fixture, first, 1).json()
    two = _ask(client, fixture, first, 2).json()
    assert one["cache"]["state"] == two["cache"]["state"] == "disabled"
    assert one["context"] == two["context"] and one["context"]["items"]
    assert calls == {first: 2}
    assert _stats(client)["context_cache_requests"] == 0
