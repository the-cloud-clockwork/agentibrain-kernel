"""Pytest fixtures — scaffold a vault + FastAPI TestClient for each test.

Tests point VAULT_ROOT at a tmp dir via monkeypatch before the app modules
are imported. The app reads env eagerly at import time, so fixtures that
want a custom root must reload the modules under test.
"""

from __future__ import annotations

import importlib
import os
from pathlib import Path

import pytest


@pytest.fixture()
def vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Fresh vault rooted at tmp_path with the real scaffold layout."""
    from agentibrain.scaffold import scaffold as run_scaffold

    run_scaffold(tmp_path)
    monkeypatch.setenv("VAULT_ROOT", str(tmp_path))
    monkeypatch.setenv("KB_ROUTER_TOKENS", "")  # disable auth in tests
    monkeypatch.setenv("KB_ROUTER_TOKEN", "")
    monkeypatch.setenv("FEED_CACHE_TTL_SECONDS", "0")
    return tmp_path


@pytest.fixture()
def client(vault: Path):
    """Reload app modules so they pick up the tmp VAULT_ROOT."""
    from fastapi.testclient import TestClient

    # Reload in dependency order so module-level env reads pick up fixture.
    for mod_name in [
        "app.feed",
        "app.signal",
        "app.markers",
        "app.tick_trigger",
        "app.main",
    ]:
        if mod_name in importlib.sys.modules:
            importlib.reload(importlib.sys.modules[mod_name])
        else:
            importlib.import_module(mod_name)

    from app.main import app

    return TestClient(app)
