"""Both outbox drains derive the idempotency key each shared vector names."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

from agentibrain import cli

ROOT = Path(__file__).resolve().parents[2]
VECTORS_FILE = ROOT / "tests/fixtures/marker-idempotency-vectors.json"
VECTORS_SHA256 = "17dce5bb059043e1df403806e910101b11a7bac1a7399feaf0656d76518789a8"
VECTORS = json.loads(VECTORS_FILE.read_text())["vectors"]


def _outbox_drain():
    spec = importlib.util.spec_from_file_location(
        "outbox_drain", ROOT / "services/brain-ops/outbox_drain.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _entry(vector: dict) -> dict:
    entry = {
        "type": vector["type"],
        "content": vector["content"] * vector["repeat"],
        "session_id": vector["session_id"],
    }
    if "recorded" in vector:
        entry["idempotency_key"] = vector["recorded"]
    return entry


def test_vectors_file_is_the_pinned_copy_shared_with_agentihooks():
    assert hashlib.sha256(VECTORS_FILE.read_bytes()).hexdigest() == VECTORS_SHA256


@pytest.mark.parametrize("vector", VECTORS, ids=[v["name"] for v in VECTORS])
def test_brain_ops_drain_key_matches_vector(vector):
    _, key = _outbox_drain()._marker_request(_entry(vector))
    assert key == vector["key"]


@pytest.mark.parametrize("vector", VECTORS, ids=[v["name"] for v in VECTORS])
def test_sync_drain_key_matches_vector(vector, tmp_path, monkeypatch):
    (tmp_path / "a.json").write_text(json.dumps(_entry(vector)))
    keys = []

    class _Created:
        status_code = 201

    def fake_post(url, headers=None, json=None, timeout=None):
        keys.append(headers["X-Idempotency-Key"])
        return _Created()

    monkeypatch.setattr(cli.httpx, "post", fake_post)
    cli._drain_marker_dir(tmp_path, "http://b", {})
    assert keys == [vector["key"]]
