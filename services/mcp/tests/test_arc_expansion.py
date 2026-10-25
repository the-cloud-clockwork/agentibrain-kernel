import asyncio
import importlib
import json
from pathlib import Path

from tests.test_kb_ranking import _Recorder


def test_semantic_arc_seeds_can_request_authorized_expansion(monkeypatch):
    app_dir = Path(__file__).resolve().parents[1] / "app"
    monkeypatch.syspath_prepend(str(app_dir))
    arcs = importlib.import_module("tools.arcs")
    monkeypatch.setattr(arcs, "EMBED_KEY", "fixture-key")
    monkeypatch.setattr(arcs, "BRAIN_API_URL", "http://brain-fixture")
    monkeypatch.setattr(arcs, "BRAIN_API_TOKEN", "fixture-grant")
    calls = []

    async def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        if url.endswith("/search"):
            return json.dumps(
                {
                    "results": [
                        {
                            "key": "seed",
                            "score": 0.9,
                            "metadata": {"title": "Seed", "projects": ["alpha"]},
                        }
                    ]
                }
            )
        assert url == "http://brain-fixture/graph/expand"
        assert kwargs["headers"] == {"Authorization": "Bearer fixture-grant"}
        assert kwargs["body"] == {"seed_ids": ["arc:seed"], "project": "alpha"}
        return json.dumps({"nodes": [{"id": "lesson:connected"}], "partial": False})

    monkeypatch.setattr(arcs, "http_request", request)
    recorder = _Recorder()
    arcs.register(recorder)
    tool = recorder.tools["brain_search_arcs"]
    baseline = json.loads(asyncio.run(tool("retry", project="alpha")))
    assert len(calls) == 1
    assert "graph" not in baseline
    expanded = json.loads(asyncio.run(tool("retry", project="alpha", expand_graph=True)))
    assert expanded["arcs"] == baseline["arcs"]
    assert expanded["graph"] == {"nodes": [{"id": "lesson:connected"}], "partial": False}
