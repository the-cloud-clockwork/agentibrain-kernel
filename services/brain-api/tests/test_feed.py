"""Tests for GET /feed — hot arcs + inject blocks + operator intent."""

from __future__ import annotations

from pathlib import Path


def _write_feed_file(vault: Path, rel_name: str, frontmatter: str, body: str) -> Path:
    path = vault / "brain-feed" / rel_name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{frontmatter}\n---\n\n{body}\n", encoding="utf-8")
    return path


def test_feed_empty_vault(client):
    resp = client.get("/feed")
    assert resp.status_code == 200
    data = resp.json()
    assert data["hot_arcs"] == []
    assert data["inject_blocks"] == []
    assert data["entry_count"] == 0
    assert "generated_at" in data
    assert data["hash"]


def test_feed_reads_hot_arcs_and_inject(vault: Path, client):
    _write_feed_file(
        vault,
        "hot-arcs.md",
        "id: hot-arcs-today\ntitle: Active Hot Arcs\npriority: 10\nttl: 3600\nseverity: info",
        "## Hot Arcs\n\n| Arc | Heat |\n|---|---|\n| [[alpha]] | 7 |\n",
    )
    _write_feed_file(
        vault,
        "inject.md",
        "id: inject\ntitle: Brain Inject\npriority: 9\nttl: 3600\nseverity: info",
        "Pay attention to alpha today.",
    )
    _write_feed_file(
        vault,
        "intent.md",
        "id: operator-intent\ntitle: Operator Intent\npriority: 7\nttl: 1800\nseverity: info",
        "Operator is migrating services to the kernel.",
    )

    resp = client.get("/feed")
    assert resp.status_code == 200
    data = resp.json()

    assert len(data["hot_arcs"]) == 1
    assert data["hot_arcs"][0]["id"] == "hot-arcs-today"
    assert data["hot_arcs"][0]["priority"] == 10
    assert "| [[alpha]]" in data["hot_arcs"][0]["content"]

    assert len(data["inject_blocks"]) == 1
    assert data["inject_blocks"][0]["id"] == "inject"

    # intent.md falls into entries (not hot, not inject)
    assert any(e["id"] == "operator-intent" for e in data["entries"])
    assert data["entry_count"] == 3


def test_feed_skips_empty_body(vault: Path, client):
    _write_feed_file(
        vault,
        "empty.md",
        "id: empty\ntitle: Empty\npriority: 5",
        "",
    )
    resp = client.get("/feed")
    assert resp.status_code == 200
    assert resp.json()["entry_count"] == 0


def test_feed_sorts_by_priority(vault: Path, client):
    _write_feed_file(vault, "low.md", "id: low\ntitle: Low\npriority: 1", "low")
    _write_feed_file(vault, "high.md", "id: high\ntitle: High\npriority: 10", "high")
    _write_feed_file(vault, "mid.md", "id: mid\ntitle: Mid\npriority: 5", "mid")

    resp = client.get("/feed")
    ordered = resp.json()["entries"]
    assert [e["id"] for e in ordered] == ["high", "mid", "low"]


def test_feed_hash_stable_across_calls(vault: Path, client):
    _write_feed_file(vault, "hot-arcs.md", "id: hot-arcs\ntitle: Hot\npriority: 10", "body")
    h1 = client.get("/feed").json()["hash"]
    h2 = client.get("/feed").json()["hash"]
    assert h1 == h2


def test_feed_bearer_token_enforced(vault: Path, monkeypatch, client):
    monkeypatch.setenv("KB_ROUTER_TOKENS", "s3cr3t")
    # Reload main to pick up token env change.
    import importlib

    from app import main as main_mod

    importlib.reload(main_mod)
    from fastapi.testclient import TestClient

    authed = TestClient(main_mod.app)
    assert authed.get("/feed").status_code == 401
    ok = authed.get("/feed", headers={"Authorization": "Bearer s3cr3t"})
    assert ok.status_code == 200
