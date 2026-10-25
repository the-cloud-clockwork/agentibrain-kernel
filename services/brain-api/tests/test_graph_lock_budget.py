import sqlite3
import time

import pytest

from tests.test_graph_expansion import _setup
from tests.test_graph_expansion_limits import _expand


@pytest.mark.parametrize("enabled", ["true", "false"])
def test_sqlite_lock_timeout_is_partial_even_when_expansion_is_disabled(
    monkeypatch, vault, tmp_path, enabled
):
    client = _setup(monkeypatch, vault, tmp_path)
    monkeypatch.setenv("BRAIN_GRAPH_EXPANSION", enabled)
    with sqlite3.connect(tmp_path / "graph.db") as writer:
        writer.execute("BEGIN EXCLUSIVE")
        try:
            response = _expand(client, time_budget_ms=10)
            assert response.status_code == 200
            data = response.json()
            assert data["partial"] is True
            assert data["reason"] == "timeout"
            assert data["nodes"] == []
            assert data["continuation"]
        finally:
            writer.rollback()
    recovered = _expand(client, continuation=data["continuation"]).json()
    assert recovered["partial"] is False
    assert recovered["nodes"][0]["id"] == "session:seed"


@pytest.mark.wall_clock
def test_sqlite_busy_wait_cannot_use_the_default_five_second_timeout(monkeypatch, vault, tmp_path):
    client = _setup(monkeypatch, vault, tmp_path)
    with sqlite3.connect(tmp_path / "graph.db") as writer:
        writer.execute("BEGIN EXCLUSIVE")
        try:
            started = time.monotonic()
            _expand(client, time_budget_ms=10)
            assert time.monotonic() - started < 1
        finally:
            writer.rollback()
