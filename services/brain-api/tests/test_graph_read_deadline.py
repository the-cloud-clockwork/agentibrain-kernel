import importlib
import sqlite3
from types import SimpleNamespace

import pytest

LONG_READ = (
    "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c WHERE x < 2000000)"
    " SELECT count(*) FROM c WHERE CASE WHEN x = 1000 THEN pass_deadline() ELSE 1 END"
)


def test_long_graph_read_is_interrupted_at_its_deadline(monkeypatch, tmp_path):
    graph = importlib.import_module("app.graph")
    clock = SimpleNamespace(now=100.0, reads_after_deadline=0)
    deadline = 101.0

    def monotonic():
        if clock.now >= deadline:
            clock.reads_after_deadline += 1
        return clock.now

    def pass_deadline():
        clock.now = deadline
        return 1

    monkeypatch.setattr(graph, "time", SimpleNamespace(monotonic=monotonic))
    store = graph.GraphStore(tmp_path / "graph.db")
    store._conn.create_function("pass_deadline", 0, pass_deadline)
    with pytest.raises(TimeoutError) as caught:
        with store.read_view(deadline) as view:
            view._execute(LONG_READ).fetchone()
    assert isinstance(caught.value.__cause__, sqlite3.OperationalError)
    assert "interrupted" in str(caught.value.__cause__)
    assert clock.reads_after_deadline == 1
    assert not store._conn.in_transaction
    clock.now = 100.0
    with store.read_view(deadline) as view:
        assert view.get_entity("arc:absent") is None
