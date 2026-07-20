"""Tests for POST /tick + GET /tick/{job_id}."""

from __future__ import annotations

import json
from pathlib import Path


def test_tick_enqueues_request_file(vault: Path, client):
    resp = client.post("/tick")
    assert resp.status_code == 202
    data = resp.json()
    assert "job_id" in data
    assert data["status"] == "pending"
    assert data["request_path"].startswith("brain-feed/ticks/requested/")

    req_file = vault / data["request_path"]
    assert req_file.exists()
    payload = json.loads(req_file.read_text())
    assert payload["job_id"] == data["job_id"]
    assert payload["dry_run"] is False
    assert payload["no_ai"] is False


def test_tick_dry_run_flag_passthrough(vault: Path, client):
    resp = client.post("/tick?dry_run=true&no_ai=true&source=test")
    assert resp.status_code == 202
    data = resp.json()
    assert data["dry_run"] is True
    assert data["no_ai"] is True

    req_file = vault / data["request_path"]
    payload = json.loads(req_file.read_text())
    assert payload["source"] == "test"


def test_tick_status_lookup_pending(vault: Path, client):
    enqueued = client.post("/tick").json()
    job_id = enqueued["job_id"]
    status = client.get(f"/tick/{job_id}").json()
    assert status["status"] == "pending"
    assert status["job_id"] == job_id


def test_tick_status_lookup_unknown(client):
    status = client.get("/tick/nonexistent-job").json()
    assert status["status"] == "unknown"


def test_tick_status_finds_completed(vault: Path, client):
    # Enqueue then manually move to completed/ to simulate tick-engine consuming it.
    enqueued = client.post("/tick").json()
    src = vault / enqueued["request_path"]
    completed_dir = vault / "brain-feed" / "ticks" / "completed"
    completed_dir.mkdir(parents=True, exist_ok=True)
    dst = completed_dir / src.name
    src.rename(dst)

    status = client.get(f"/tick/{enqueued['job_id']}").json()
    assert status["status"] == "completed"
    assert status["path"].startswith("brain-feed/ticks/completed/")


def test_tick_always_writes_no_enqueue_dedupe(vault: Path, client):
    # Enqueue never coalesces — that is the drain's job (single serialized
    # process). Enqueue-time dedupe raced under FastAPI's threadpool and could
    # wedge on an orphaned request file, so every call writes a fresh request
    # with its own job_id. Two rapid identical calls therefore leave two files.
    first = client.post("/tick")
    second = client.post("/tick")
    assert first.status_code == 202
    assert second.status_code == 202
    assert first.json()["job_id"] != second.json()["job_id"]
    assert "duplicate" not in first.json()

    req_dir = vault / "brain-feed" / "ticks" / "requested"
    assert len(list(req_dir.glob("*.json"))) == 2
