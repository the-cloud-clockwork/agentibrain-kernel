"""Tests for POST /marker — four marker types + idempotency."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path


def test_marker_rejects_invalid_type(client):
    resp = client.post("/marker", json={"type": "gossip", "content": "..."})
    assert resp.status_code == 400


def test_marker_rejects_empty_content(client):
    resp = client.post("/marker", json={"type": "lesson", "content": ""})
    assert resp.status_code == 400


def test_marker_lesson_appends(vault: Path, client):
    body = {
        "type": "lesson",
        "content": "NFS dirs created by root need chmod 777 for UID 1000 writers.",
        "attrs": {"source": "deploy", "session_id": "abc123"},
    }
    resp = client.post("/marker", json=body)
    assert resp.status_code == 201
    data = resp.json()
    assert data["ok"] is True
    assert data["marker_type"] == "lesson"
    assert data["action"] == "appended"
    today = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
    assert data["vault_path"] == f"left/reference/lessons-{today}.md"

    written = (vault / data["vault_path"]).read_text()
    assert "NFS dirs" in written
    assert "abc123" in written

    # Second lesson appends (not overwrites).
    body2 = {"type": "lesson", "content": "Second lesson.", "attrs": {"source": "repro"}}
    resp2 = client.post("/marker", json=body2)
    assert resp2.status_code == 201
    assert (vault / data["vault_path"]).read_text().count("##") >= 2


def test_marker_milestone_daily_fallback(vault: Path, client):
    body = {
        "type": "milestone",
        "content": "Stream 1A shipped.",
        "attrs": {"source": "phase-7", "status": "done", "scope": "kb-router"},
    }
    resp = client.post("/marker", json=body)
    assert resp.status_code == 201
    data = resp.json()
    today = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
    # No matching project dir → falls through to daily/.
    assert data["vault_path"] == f"daily/{today}.md"
    assert "Stream 1A" in (vault / data["vault_path"]).read_text()


def test_marker_milestone_routes_to_project_when_present(vault: Path, client):
    (vault / "left" / "projects" / "phase-7").mkdir(parents=True)
    body = {"type": "milestone", "content": "Stream shipped.", "attrs": {"source": "phase-7"}}
    resp = client.post("/marker", json=body)
    data = resp.json()
    assert data["vault_path"] == "left/projects/phase-7/BLOCKS.md"
    assert (vault / data["vault_path"]).exists()


def test_marker_signal_creates_new_file(vault: Path, client):
    body = {
        "type": "signal",
        "content": "Disk full on host-01.",
        "attrs": {"severity": "critical", "source": "ops"},
    }
    resp = client.post("/marker", json=body)
    assert resp.status_code == 201
    data = resp.json()
    assert data["marker_type"] == "signal"
    assert data["action"] == "created"
    assert data["vault_path"].startswith("amygdala/")
    assert "critical" in data["vault_path"]
    content = (vault / data["vault_path"]).read_text()
    assert "severity: critical" in content


def test_marker_decision_increments_adr_number(vault: Path, client):
    body = {"type": "decision", "content": "Adopt kernel v0.1.0 tag for install pinning."}
    r1 = client.post("/marker", json=body).json()
    r2 = client.post("/marker", json={**body, "attrs": {"title": "second"}}).json()
    assert r1["vault_path"].startswith("left/decisions/ADR-0001-")
    assert r2["vault_path"].startswith("left/decisions/ADR-0002-")


def test_marker_idempotency_replays_cached_response(vault: Path, client):
    body = {"type": "lesson", "content": "Only write this once.", "attrs": {"source": "test"}}
    key = "test-idempotency-1"
    r1 = client.post("/marker", json=body, headers={"X-Idempotency-Key": key}).json()
    r2 = client.post("/marker", json=body, headers={"X-Idempotency-Key": key}).json()
    assert r1["vault_path"] == r2["vault_path"]
    assert r2.get("idempotent_replay") is True
    # File appended exactly once.
    assert (vault / r1["vault_path"]).read_text().count("##") == 1


def test_marker_content_size_limit(client):
    body = {"type": "lesson", "content": "x" * 5000, "attrs": {}}
    resp = client.post("/marker", json=body)
    assert resp.status_code == 400
