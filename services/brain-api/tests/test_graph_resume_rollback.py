import sqlite3

import pytest

from tests.test_graph_expansion import _setup
from tests.test_graph_expansion_limits import _expand, _timeout


@pytest.mark.parametrize("enabled", ["true", "false"])
def test_resumption_cannot_disclose_cached_nodes_without_a_validated_snapshot(
    monkeypatch, vault, tmp_path, enabled
):
    client = _setup(monkeypatch, vault, tmp_path)
    partial, _ = _timeout(monkeypatch, client)
    monkeypatch.setenv("BRAIN_GRAPH_EXPANSION", enabled)
    with sqlite3.connect(tmp_path / "graph.db") as writer:
        writer.execute("BEGIN EXCLUSIVE")
        try:
            response = _expand(client, continuation=partial["continuation"], time_budget_ms=1)
            assert response.status_code == 200
            locked = response.json()
            assert locked["partial"] is True
            assert locked["reason"] == "timeout"
            assert locked["nodes"] == []
            assert locked["graph_nodes_expanded_per_request"] == 0
            assert locked["continuation"]
        finally:
            writer.rollback()
    recovered = _expand(client, continuation=locked["continuation"]).json()
    assert recovered["partial"] is False
    assert recovered["graph_nodes_expanded_per_request"] >= 0
    expected = (
        ["session:seed"] if enabled == "false" else ["session:seed", "arc:bridge", "lesson:answer"]
    )
    assert [node["id"] for node in recovered["nodes"]] == expected
