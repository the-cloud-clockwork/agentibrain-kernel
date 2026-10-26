"""SV2-MUL-04: brain-api refuses to serve a vault another brain claims."""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_MODULES = [
    "app.scope",
    "app.instance_claim",
    "app.feed",
    "app.signal",
    "app.markers",
    "app.tick_trigger",
    "app.pipeline",
    "app.main",
]


def _app(monkeypatch, brain_id: str | None):
    if brain_id is None:
        monkeypatch.delenv("BRAIN_ID", raising=False)
    else:
        monkeypatch.setenv("BRAIN_ID", brain_id)
    monkeypatch.delenv("KB_WORKER_TOKENS", raising=False)
    for name in _MODULES:
        if name in importlib.sys.modules:
            importlib.reload(importlib.sys.modules[name])
        else:
            importlib.import_module(name)
    from app.main import app

    return app


def _claim(vault: Path) -> dict:
    return json.loads((vault / ".brain-instance").read_text())


def test_starting_claims_an_unclaimed_vault(monkeypatch, vault):
    with TestClient(_app(monkeypatch, "personal")) as client:
        assert client.get("/health").status_code == 200

    assert _claim(vault)["brain_id"] == "personal"


def test_the_default_brain_claims_as_default(monkeypatch, vault):
    with TestClient(_app(monkeypatch, None)):
        pass

    assert _claim(vault)["brain_id"] == "default"


def test_a_vault_claimed_by_another_brain_refuses_to_start(monkeypatch, vault):
    with TestClient(_app(monkeypatch, "personal")):
        pass
    before = (vault / ".brain-instance").read_bytes()

    with pytest.raises(RuntimeError, match="belongs to brain personal"):
        with TestClient(_app(monkeypatch, "swarm")):
            pass

    assert (vault / ".brain-instance").read_bytes() == before


def test_the_claim_file_is_not_served_as_vault_content(monkeypatch, vault):
    with TestClient(_app(monkeypatch, "personal")) as client:
        listing = client.get("/vault/list", headers={"Authorization": "Bearer test-bearer"})

    assert listing.status_code == 200
    assert ".brain-instance" not in listing.text
