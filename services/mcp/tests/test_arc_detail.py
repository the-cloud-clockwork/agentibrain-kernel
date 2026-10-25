import asyncio
import importlib
import json
from pathlib import Path

from .test_kb_ranking import _Recorder


def test_full_detail_uses_only_authoritative_endpoint(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "app"))
    arcs = importlib.import_module("tools.arcs")
    monkeypatch.setattr(arcs, "BRAIN_API_URL", "http://brain-fixture")
    monkeypatch.setattr(arcs, "BRAIN_API_TOKEN", "fixture-grant")
    calls = []

    async def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        assert method == "GET"
        assert url == "http://brain-fixture/knowledge/detail"
        assert kwargs["headers"] == {"Authorization": "Bearer fixture-grant"}
        assert kwargs["params"] == {
            "entity_id": "lesson:exact",
            "revision": 3,
            "section": "body",
            "section_offset": 1,
            "reference_offset": 2,
            "content_hash": "expected",
            "section_limit": 5,
            "reference_limit": 5,
        }
        return json.dumps({"error": "detail revision unavailable"})

    monkeypatch.setattr(arcs, "http_request", request)
    recorder = _Recorder()
    arcs.register(recorder)
    read = recorder.tools["brain_read_detail"]
    result = asyncio.run(read("lesson:exact", 3, "body", 1, 2, "expected", 5))
    assert json.loads(result) == {"error": "detail revision unavailable"}
    assert len(calls) == 1


def test_legacy_indexed_preview_is_never_labelled_complete(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "app"))
    arcs = importlib.import_module("tools.arcs")
    monkeypatch.setattr(arcs, "EMBED_KEY", "fixture-key")

    async def request(*args, **kwargs):
        return json.dumps({"chunks": [{"text_preview": "indexed excerpt"}]})

    monkeypatch.setattr(arcs, "http_request", request)
    recorder = _Recorder()
    arcs.register(recorder)
    preview = recorder.tools["brain_get_arc"]
    result = json.loads(asyncio.run(preview("long")))
    assert result["text_preview"] == "indexed excerpt"
    assert result["complete"] is False
    assert result["representation"] == "preview"
    assert "preview" in preview.__doc__.lower()
