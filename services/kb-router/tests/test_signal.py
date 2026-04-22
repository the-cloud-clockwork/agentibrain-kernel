"""Tests for GET /signal — single-file amygdala state."""

from __future__ import annotations

from pathlib import Path


def _write_signal(vault: Path, frontmatter: str, body: str) -> Path:
    path = vault / "brain-feed" / "amygdala-active.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{frontmatter}\n---\n\n{body}\n", encoding="utf-8")
    return path


def test_signal_absent_returns_inactive(client):
    resp = client.get("/signal")
    assert resp.status_code == 200
    data = resp.json()
    assert data["active"] is False
    assert data["severity"] is None
    assert data["content"] is None
    assert data["hash"] is None


def test_signal_active_with_severity(vault: Path, client):
    _write_signal(
        vault,
        "id: amygdala-alpha\ntitle: Deploy broke prod\nseverity: critical",
        "publisher-0 is crashlooping on ImagePullBackOff.",
    )
    resp = client.get("/signal")
    assert resp.status_code == 200
    data = resp.json()
    assert data["active"] is True
    assert data["severity"] == "critical"
    assert data["title"] == "Deploy broke prod"
    assert "crashlooping" in data["content"]
    assert data["hash"]
    assert data["last_updated"]


def test_signal_resolved_marks_inactive(vault: Path, client):
    _write_signal(
        vault,
        "id: amygdala-alpha\ntitle: fixed\nseverity: resolved",
        "prior alert resolved.",
    )
    data = client.get("/signal").json()
    assert data["active"] is False
    assert data["severity"] == "resolved"


def test_signal_hash_changes_on_body_change(vault: Path, client):
    _write_signal(vault, "id: a\ntitle: one\nseverity: critical", "first")
    h1 = client.get("/signal").json()["hash"]
    _write_signal(vault, "id: a\ntitle: one\nseverity: critical", "second")
    h2 = client.get("/signal").json()["hash"]
    assert h1 and h2 and h1 != h2


def test_signal_unknown_severity_coerced_to_critical(vault: Path, client):
    _write_signal(vault, "id: a\ntitle: junk\nseverity: apocalypse", "body")
    assert client.get("/signal").json()["severity"] == "critical"
