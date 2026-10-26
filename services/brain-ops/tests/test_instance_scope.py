"""SV2-MUL-04: brain-ops keeps each brain's vault, event stream and Redis keys apart.

Redis is a dict-backed fake: these cases prove the key names and the claim
protocol, not a live server.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

HERE = Path(__file__).resolve().parents[1]


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"{name}_scope_test", HERE / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeRedis:
    def __init__(self, store: dict):
        self.store = store
        self.added: list[tuple[str, dict]] = []
        self.groups: list[tuple[str, str]] = []

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True

    def get(self, key):
        return self.store.get(key)

    def xadd(self, stream, payload, **kwargs):
        self.added.append((stream, payload))

    def xgroup_create(self, stream, group, id="0", mkstream=False):
        self.groups.append((stream, group))

    def xreadgroup(self, *args, **kwargs):
        return []

    def delete(self, key):
        self.store.pop(key, None)


def _redis_module(amygdala, monkeypatch, client: FakeRedis) -> None:
    monkeypatch.setattr(
        amygdala,
        "redis",
        SimpleNamespace(
            Redis=SimpleNamespace(from_url=lambda *a, **kw: client),
            retry=SimpleNamespace(Retry=lambda backoff, retries: None),
            backoff=SimpleNamespace(NoBackoff=lambda: None),
            exceptions=SimpleNamespace(ResponseError=type("ResponseError", (Exception,), {})),
        ),
    )


@pytest.fixture
def brain(monkeypatch):
    def select(brain_id: str | None):
        if brain_id is None:
            monkeypatch.delenv("BRAIN_ID", raising=False)
        else:
            monkeypatch.setenv("BRAIN_ID", brain_id)
        for var in ("AMYGDALA_STREAMS", "EVENT_BUS_STREAM"):
            monkeypatch.delenv(var, raising=False)
        return _load("instance_scope")

    return select


def test_the_default_brain_keeps_legacy_keys(brain):
    scope = brain(None)

    assert scope.namespaced("events:brain") == "events:brain"
    assert scope.namespaced("amygdala") == "amygdala"


def test_a_named_brain_prefixes_every_key_it_owns(brain):
    scope = brain("swarm")

    assert scope.namespaced("events:brain") == "brain:swarm:events:brain"
    assert scope.namespaced("amygdala:last_event_ts") == "brain:swarm:amygdala:last_event_ts"


def test_a_vault_claimed_by_another_brain_refuses_without_writing(brain, tmp_path):
    brain("personal").claim_vault(tmp_path)
    claim = tmp_path / ".brain-instance"
    before = claim.read_bytes()

    scope = brain("swarm")
    with pytest.raises(scope.ScopeError, match="belongs to brain personal"):
        scope.claim_vault(tmp_path)

    assert claim.read_bytes() == before
    assert json.loads(before)["brain_id"] == "personal"


def test_a_stream_claimed_by_another_brain_refuses(brain):
    store: dict = {}
    brain("personal").claim_stream(FakeRedis(store), "events:brain")

    scope = brain("swarm")
    with pytest.raises(scope.ScopeError):
        scope.claim_stream(FakeRedis(store), "events:brain")

    assert store == {"events:brain:owner": "personal"}


def test_two_named_tick_announcements_land_on_separate_streams(brain, monkeypatch):
    store: dict = {}
    clients: list[FakeRedis] = []
    redis = SimpleNamespace(
        Redis=SimpleNamespace(
            from_url=lambda *a, **kw: clients.append(FakeRedis(store)) or clients[-1]
        )
    )
    monkeypatch.setitem(__import__("sys").modules, "redis", redis)
    monkeypatch.setenv("REDIS_URL", "redis://cache.shared:6379/0")
    for name in ("personal", "swarm"):
        brain(name)
        _load("brain_tick")._push_event_bus({"phases": {}})

    assert [stream for c in clients for stream, _ in c.added] == [
        "brain:personal:events:brain",
        "brain:swarm:events:brain",
    ]
    assert store == {
        "brain:personal:events:brain:owner": "personal",
        "brain:swarm:events:brain:owner": "swarm",
    }


def test_a_misbound_tick_announcement_is_not_written(brain, monkeypatch, capsys):
    store = {"events:brain:owner": "personal"}
    client = FakeRedis(store)
    redis = SimpleNamespace(Redis=SimpleNamespace(from_url=lambda *a, **kw: client))
    monkeypatch.setitem(__import__("sys").modules, "redis", redis)
    monkeypatch.setenv("REDIS_URL", "redis://cache.shared:6379/0")
    brain("swarm")
    monkeypatch.setenv("EVENT_BUS_STREAM", "events:brain")

    _load("brain_tick")._push_event_bus({"phases": {}})

    assert client.added == []
    assert "belongs to brain personal" in capsys.readouterr().out


def test_amygdala_reads_with_its_own_group_and_stream(brain, monkeypatch, tmp_path):
    brain("swarm")
    amygdala = _load("amygdala")
    store: dict = {}
    client = FakeRedis(store)
    _redis_module(amygdala, monkeypatch, client)

    amygdala.consume("redis://cache.shared:6379/11", tmp_path, tmp_path / "brain-feed")

    assert "brain:swarm:events:brain" in amygdala.STREAMS
    assert "events:brain" not in amygdala.STREAMS
    assert {group for _, group in client.groups} == {"brain:swarm:amygdala"}
    assert store["brain:swarm:events:brain:owner"] == "swarm"


def test_amygdala_refuses_a_brain_stream_claimed_by_another_brain(brain, monkeypatch, tmp_path):
    brain("swarm")
    monkeypatch.setenv("AMYGDALA_STREAMS", "events:health,events:brain")
    monkeypatch.setenv("EVENT_BUS_STREAM", "events:brain")
    amygdala = _load("amygdala")
    client = FakeRedis({"events:brain:owner": "personal"})
    _redis_module(amygdala, monkeypatch, client)

    with pytest.raises(RuntimeError, match="belongs to brain personal"):
        amygdala.consume("redis://cache.shared:6379/11", tmp_path, tmp_path / "brain-feed")

    assert client.groups == []


def test_a_tick_cycle_on_another_brains_vault_refuses(brain, tmp_path, capsys):
    brain("personal").claim_vault(tmp_path)
    brain("swarm")

    assert _load("tick_cycle").main(["drain", "--vault", str(tmp_path)]) == 2
    assert "belongs to brain personal" in capsys.readouterr().out
