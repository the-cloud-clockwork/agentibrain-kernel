"""The maintenance tick spends synthesis from its own brain's inference budget."""

from __future__ import annotations

import io
import json
import urllib.error
from pathlib import Path

import brain_apply
import brain_keeper
import brain_tick
import brain_tick_prompt
import pytest

GATEWAY = "http://inference.fixture"
BRAIN_API = "http://swarm-brain-api.fixture"


class _Reply(io.BytesIO):
    def __enter__(self) -> _Reply:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _tick(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, answer: object) -> tuple[dict, list]:
    sent: list = []
    applied: list = []

    def _urlopen(request: object, timeout: float) -> _Reply:
        sent.append((request.full_url, json.loads(request.data), dict(request.header_items())))
        if isinstance(answer, int):
            raise urllib.error.HTTPError(request.full_url, answer, "refused", {}, None)
        return _Reply(json.dumps(answer).encode())

    monkeypatch.setattr(brain_tick.urllib.request, "urlopen", _urlopen)
    monkeypatch.setattr(brain_keeper, "tick", lambda *a, **k: {})
    monkeypatch.setattr(brain_tick_prompt, "build_prompt", lambda *a, **k: ("prompt", None))
    monkeypatch.setattr(brain_apply, "apply", lambda _v, _f, out, **k: applied.append(out) or {})
    monkeypatch.setattr(brain_tick, "CLICKHOUSE_URL", "")
    monkeypatch.setattr(brain_tick, "EVENT_BUS_REDIS_URL", "")
    feed = tmp_path / "brain-feed"
    feed.mkdir()
    report = brain_tick.run_tick(tmp_path, feed, inference_url=GATEWAY)
    report["applied"] = applied
    return report, sent


def test_a_spent_swarm_budget_defers_the_tick_without_a_gateway_call(monkeypatch, tmp_path):
    monkeypatch.setenv("BRAIN_ID", "swarm")
    monkeypatch.setenv("BRAIN_API_URL", BRAIN_API)
    monkeypatch.setenv("KB_ROUTER_TOKEN", "router-fixture")

    report, sent = _tick(monkeypatch, tmp_path, 429)

    assert [url for url, _, _ in sent] == [f"{BRAIN_API}/v2/inference/synthesis"]
    assert sent[0][1]["brain_id"] == "swarm"
    assert sent[0][2]["Authorization"] == "Bearer router-fixture"
    assert report["phases"]["ai"]["deferred"] is True
    assert report["phases"]["ai"]["error"] is False
    assert report["applied"] == []


def test_a_granted_swarm_synthesis_is_applied(monkeypatch, tmp_path):
    monkeypatch.setenv("BRAIN_ID", "swarm")
    monkeypatch.setenv("BRAIN_API_URL", BRAIN_API)

    report, sent = _tick(monkeypatch, tmp_path, {"brain_id": "swarm", "content": "brief"})

    assert [url for url, _, _ in sent] == [f"{BRAIN_API}/v2/inference/synthesis"]
    body = sent[0][1]
    assert body["brain_id"] == "swarm"
    assert body["model"] == brain_tick.BRAIN_BRIEF_MODEL
    assert body["prompt"] == "prompt"
    assert body["max_tokens"] == brain_tick.BRAIN_LLM_MAX_TOKENS
    assert report["phases"]["ai"]["deferred"] is False
    assert report["applied"] == ["brief"]


def test_a_named_brain_without_its_brain_api_defers(monkeypatch, tmp_path):
    monkeypatch.setenv("BRAIN_ID", "swarm")
    monkeypatch.delenv("BRAIN_API_URL", raising=False)

    report, sent = _tick(monkeypatch, tmp_path, {"choices": [{"message": {"content": "x"}}]})

    assert sent == []
    assert report["phases"]["ai"]["deferred"] is True
    assert report["applied"] == []


def test_the_default_brain_without_a_brain_api_keeps_the_gateway(monkeypatch, tmp_path):
    monkeypatch.delenv("BRAIN_ID", raising=False)
    monkeypatch.delenv("BRAIN_API_URL", raising=False)

    report, sent = _tick(monkeypatch, tmp_path, {"choices": [{"message": {"content": "brief"}}]})

    assert [url for url, _, _ in sent] == [f"{GATEWAY}/chat/completions"]
    assert report["phases"]["ai"]["deferred"] is False
    assert report["applied"] == ["brief"]


def test_a_brain_api_failure_is_an_ai_error(monkeypatch, tmp_path):
    monkeypatch.setenv("BRAIN_ID", "swarm")
    monkeypatch.setenv("BRAIN_API_URL", BRAIN_API)

    report, sent = _tick(monkeypatch, tmp_path, 502)

    assert len(sent) == 1
    assert report["phases"]["ai"]["error"] is True
    assert report["phases"]["ai"]["deferred"] is False
    assert report["applied"] == []
