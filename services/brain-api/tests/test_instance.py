"""brain-api names the brain it serves before any credential is sent."""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agentibrain import client as brain_client
from agentibrain.client import BrainBindingError, BrainClient

TEST_BEARER = "test-bearer"

_MODULES = [
    "app.scope",
    "app.feed",
    "app.signal",
    "app.markers",
    "app.tick_trigger",
    "app.pipeline",
    "app.main",
]


def _serve(
    monkeypatch, vault: Path, brain_id: str | None, revision: str | None = None
) -> TestClient:
    if brain_id is None:
        monkeypatch.delenv("BRAIN_ID", raising=False)
    else:
        monkeypatch.setenv("BRAIN_ID", brain_id)
    if revision is None:
        monkeypatch.delenv("BRAIN_POLICY_REVISION", raising=False)
    else:
        monkeypatch.setenv("BRAIN_POLICY_REVISION", revision)
    monkeypatch.delenv("KB_WORKER_TOKENS", raising=False)
    for name in _MODULES:
        if name in importlib.sys.modules:
            importlib.reload(importlib.sys.modules[name])
        else:
            importlib.import_module(name)
    from app.main import app

    return TestClient(app)


@pytest.fixture(autouse=True)
def _fresh_counter():
    brain_client.brain_instance_binding_failures.clear()


def test_an_unnamed_brain_answers_default_without_credentials(monkeypatch, vault):
    response = _serve(monkeypatch, vault, None).get("/instance")

    assert response.status_code == 200
    assert response.json() == {
        "schema": "brain-instance-v2",
        "brain_id": "default",
        "policy_revision": 1,
    }


def test_a_named_brain_answers_its_name_and_policy_revision(monkeypatch, vault):
    body = _serve(monkeypatch, vault, "swarm", "3").get("/instance").json()

    assert body == {"schema": "brain-instance-v2", "brain_id": "swarm", "policy_revision": 3}
    assert str(vault) not in str(body)
    assert TEST_BEARER not in str(body)


def test_a_client_binds_to_the_brain_it_expects(monkeypatch, vault):
    for name in ("personal", "swarm"):
        server = _serve(monkeypatch, vault, name)
        brain = BrainClient(
            "http://testserver", TEST_BEARER, expected_brain_id=name, transport=server._transport
        )

        assert brain.verify_instance()["brain_id"] == name
        assert brain.health()["status"] == "ok"
    assert sum(brain_client.brain_instance_binding_failures.values()) == 0


def test_a_client_expecting_another_brain_is_refused_and_writes_nothing(monkeypatch, vault):
    server = _serve(monkeypatch, vault, "personal")
    before = sorted(str(p.relative_to(vault)) for p in vault.rglob("*"))
    brain = BrainClient(
        "http://testserver", TEST_BEARER, expected_brain_id="swarm", transport=server._transport
    )

    with pytest.raises(BrainBindingError) as refused:
        brain.health()

    assert "personal" in str(refused.value)
    assert TEST_BEARER not in str(refused.value)
    assert sorted(str(p.relative_to(vault)) for p in vault.rglob("*")) == before
    assert brain_client.brain_instance_binding_failures == {("swarm", "personal"): 1}


def test_a_brain_id_outside_the_schema_refuses_to_start(monkeypatch, vault):
    monkeypatch.setenv("BRAIN_ID", "github.com/org/repo")

    with pytest.raises(RuntimeError, match="BRAIN_ID"):
        importlib.reload(importlib.import_module("app.scope"))

    monkeypatch.delenv("BRAIN_ID")
    importlib.reload(importlib.import_module("app.scope"))
