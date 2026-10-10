"""A marker replayed from an agentihooks outbox file is written once."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from agentibrain import cli

ROOT = Path(__file__).resolve().parents[3]
NAMESPACED_KEY = "5f0c2a9e41b7d36c8e01f4a2b9d7c653"
ENTRY = {
    "type": "milestone",
    "content": "Replayed outbox milestone lands once.",
    "attrs": {"source": "replay-test"},
    "session_id": "sess-replay",
    "idempotency_key": NAMESPACED_KEY,
}


def _outbox_drain():
    spec = importlib.util.spec_from_file_location(
        "outbox_drain", ROOT / "services/brain-ops/outbox_drain.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sync_drain(outbox: Path, client, monkeypatch) -> dict:
    def post(url, headers=None, json=None, timeout=None):
        return client.post("/marker", headers=headers, json=json)

    monkeypatch.setattr(cli.httpx, "post", post)
    return cli._drain_marker_dir(outbox, "http://brain", {})


def _brain_ops_drain(outbox: Path, client, monkeypatch) -> dict:
    drain = _outbox_drain()

    def post(brain_url, token, body, idem):
        client.post("/marker", headers={"X-Idempotency-Key": idem}, json=body).raise_for_status()

    monkeypatch.setattr(drain, "_post_marker", post)
    return drain.drain_dir(outbox, "http://brain", "token")


@pytest.mark.parametrize("drain", [_sync_drain, _brain_ops_drain], ids=["sync", "brain-ops"])
def test_replayed_marker_is_not_duplicated(drain, vault, client, tmp_path_factory, monkeypatch):
    first = client.post(
        "/marker",
        headers={"X-Idempotency-Key": NAMESPACED_KEY},
        json={
            "type": ENTRY["type"],
            "content": ENTRY["content"],
            "attrs": {**ENTRY["attrs"], "session_id": ENTRY["session_id"]},
        },
    )
    assert first.status_code == 201
    outbox = tmp_path_factory.mktemp("outbox")
    (outbox / "marker.json").write_text(json.dumps(ENTRY))

    assert drain(outbox, client, monkeypatch)["drained"] == 1
    assert list(outbox.iterdir()) == []
    written = (vault / first.json()["vault_path"]).read_text()
    assert written.count(ENTRY["content"]) == 1
