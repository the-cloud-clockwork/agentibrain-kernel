"""GET /feed?project= serves each project's own feed, never a filtered global hot set.

The snapshots are the files brain-ops writes for a dominant project with many
hot arcs and a quieter project with one cold but active task, in two
independent sets. These tests are mocked evidence: no tick runs.
"""

from __future__ import annotations

import hashlib
import importlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_MODULES = [
    "app.feed",
    "app.signal",
    "app.markers",
    "app.tick_trigger",
    "app.pipeline",
    "app.scope",
    "app.main",
]
ROUTER = "router-bearer"
SETS = {
    "alpha": {
        "dominant": "github.com/acme/dominant-alpha",
        "quiet": "github.com/acme/quiet-alpha",
        "quiet_arc": "quiet-alpha-cold-task",
        "fleet": "Alpha fleet: the shared runner pool is at capacity.",
    },
    "beta": {
        "dominant": "gitlab.example/team/dominant-beta",
        "quiet": "gitlab.example/team/quiet-beta",
        "quiet_arc": "quiet-beta-cold-task",
        "fleet": "Beta fleet: the shared object store is read only.",
    },
}


def _key(project_id: str) -> str:
    return hashlib.sha256(project_id.encode("utf-8")).hexdigest()[:24]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(when: datetime) -> str:
    return when.isoformat(timespec="seconds")


def _arc(project: str, ref: str, heat: int) -> dict:
    return {
        "ref": f"arc:{ref}",
        "projects": [project],
        "title": ref,
        "status": "active",
        "heat": heat,
        "task_id": f"task-{ref}",
    }


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _snapshot(vault: Path, project: str, arcs: list[dict], when: datetime, **extra) -> None:
    data = {
        "schema": 1,
        "project_id": project,
        "generated_at": _iso(when),
        "revision": hashlib.sha256(json.dumps(arcs).encode()).hexdigest()[:16],
        "active": True,
        "arcs": arcs,
        "lessons": [],
        "decisions": [],
        "signals": [],
    }
    data.update(extra)
    _write_json(vault / "brain-feed" / "projects" / f"{_key(project)}.json", data)


def _intent(vault: Path, project: str, text: str, state: str, when: datetime) -> None:
    _write_json(
        vault / "brain-feed" / "project-intent" / f"{_key(project)}.json",
        {
            "schema": 1,
            "project_id": project,
            "text": text,
            "label": "inferred",
            "state": state,
            "inferred_at": _iso(when),
        },
    )


def _seed(vault: Path, fx: dict) -> None:
    now = _now()
    dominant = [_arc(fx["dominant"], f"dominant-{n:02d}", 10 - n % 4) for n in range(10)]
    _snapshot(vault, fx["dominant"], dominant, now)
    _snapshot(vault, fx["quiet"], [_arc(fx["quiet"], fx["quiet_arc"], 0)], now)
    _write_json(
        vault / "brain-feed" / "fleet-warnings.json",
        {
            "schema": 1,
            "generated_at": _iso(now),
            "warnings": [
                {
                    "ref": "signal:fleet",
                    "scope": "fleet",
                    "severity": "warning",
                    "content": fx["fleet"],
                },
                {
                    "ref": "signal:leak",
                    "scope": "project",
                    "severity": "warning",
                    "content": "leak",
                },
            ],
        },
    )
    hot = vault / "brain-feed" / "hot-arcs.md"
    hot.write_text(
        "---\nid: hot-arcs-today\ntitle: Active Hot Arcs\npriority: 10\n---\n\n"
        "| [[dominant-00]] | 10 |\n",
        encoding="utf-8",
    )


def _client(monkeypatch, vault: Path, grants: str = "") -> TestClient:
    from agentibrain.scaffold import scaffold as run_scaffold

    run_scaffold(vault)
    monkeypatch.setenv("VAULT_ROOT", str(vault))
    monkeypatch.setenv("KB_ROUTER_TOKENS", ROUTER)
    monkeypatch.setenv("FEED_CACHE_TTL_SECONDS", "0")
    monkeypatch.setenv("BRAIN_ID", "hive-a")
    monkeypatch.setenv("KB_WORKER_TOKENS", grants or "hive-a=worker-a")
    for name in _MODULES:
        if name in importlib.sys.modules:
            importlib.reload(importlib.sys.modules[name])
        else:
            importlib.import_module(name)
    from app.main import app

    return TestClient(app)


def _get(client: TestClient, project: str | None, token: str = ROUTER):
    params = {"project": project} if project is not None else {}
    return client.get("/feed", params=params, headers={"Authorization": f"Bearer {token}"})


def _snapshot_files(vault: Path) -> dict[str, str]:
    feed = vault / "brain-feed"
    return {
        str(p.relative_to(feed)): p.read_text(encoding="utf-8")
        for p in sorted(feed.rglob("*"))
        if p.is_file()
    }


@pytest.fixture(params=sorted(SETS))
def fx(request) -> dict:
    return SETS[request.param]


def test_two_repositories_get_different_feeds_even_outside_the_fleet_top_ten(
    monkeypatch, tmp_path, fx
):
    vault = tmp_path / "vault"
    client = _client(monkeypatch, vault)
    _seed(vault, fx)

    quiet = _get(client, fx["quiet"])
    dominant = _get(client, fx["dominant"])

    assert quiet.status_code == 200
    assert dominant.status_code == 200
    q, d = quiet.json(), dominant.json()
    assert q["scope"] == "project"
    assert q["project_id"] == fx["quiet"]
    assert q["brain_id"] == "hive-a"
    assert [a["ref"] for a in q["arcs"]] == [f"arc:{fx['quiet_arc']}"]
    assert len(d["arcs"]) == 10
    assert all(a["ref"].startswith("arc:dominant-") for a in d["arcs"])
    assert q["revision"] != d["revision"]
    assert q["freshness"]["state"] == "fresh"
    assert q["metrics"] == {"project_feed_cross_scope_failures": 0}
    assert [w["content"] for w in q["fleet_warnings"]] == [fx["fleet"]]
    assert "hot_arcs" not in q


def test_the_revision_is_stable_until_the_snapshot_changes(monkeypatch, tmp_path, fx):
    vault = tmp_path / "vault"
    client = _client(monkeypatch, vault)
    _seed(vault, fx)

    first = _get(client, fx["quiet"]).json()
    second = _get(client, fx["quiet"]).json()
    _snapshot(vault, fx["quiet"], [_arc(fx["quiet"], "quiet-new", 1)], _now())
    third = _get(client, fx["quiet"]).json()

    assert first["revision"] == second["revision"]
    assert third["revision"] != first["revision"]


def test_the_unscoped_feed_stays_for_legacy_clients(monkeypatch, tmp_path, fx):
    vault = tmp_path / "vault"
    client = _client(monkeypatch, vault)
    _seed(vault, fx)

    legacy = _get(client, None)

    assert legacy.status_code == 200
    assert [e["id"] for e in legacy.json()["hot_arcs"]] == ["hot-arcs-today"]


def test_missing_project_data_returns_a_scoped_empty_result(monkeypatch, tmp_path, fx):
    vault = tmp_path / "vault"
    client = _client(monkeypatch, vault)
    _seed(vault, fx)
    before = _snapshot_files(vault)

    response = _get(client, "github.com/acme/no-such-project")

    assert response.status_code == 200
    data = response.json()
    assert data["project_id"] == "github.com/acme/no-such-project"
    assert data["scope"] == "project"
    for kind in ("arcs", "lessons", "decisions", "signals"):
        assert data[kind] == []
    assert data["intent"] is None
    assert data["freshness"] == {"state": "missing", "generated_at": None, "age_seconds": None}
    assert "dominant" not in json.dumps(data)
    assert _snapshot_files(vault) == before


def test_a_grant_outside_the_project_is_refused_and_recorded(monkeypatch, tmp_path, fx):
    vault = tmp_path / "vault"
    client = _client(monkeypatch, vault, f"hive-a@{fx['quiet']}=worker-q")
    _seed(vault, fx)

    allowed = _get(client, fx["quiet"], "worker-q")
    refused = _get(client, fx["dominant"], "worker-q")
    unscoped = _get(client, None, "worker-q")

    assert allowed.status_code == 200
    assert refused.status_code == 403
    assert "dominant" not in refused.text
    assert unscoped.status_code == 400
    denials = client.get("/denials", headers={"Authorization": f"Bearer {ROUTER}"}).json()
    assert [d["query"] for d in denials["denials"]] == ["GET /feed"]


def test_items_from_another_project_are_dropped_and_counted(monkeypatch, tmp_path, fx):
    vault = tmp_path / "vault"
    client = _client(monkeypatch, vault)
    _seed(vault, fx)
    leaked = _arc(fx["dominant"], "dominant-leak", 9)
    _snapshot(
        vault,
        fx["quiet"],
        [_arc(fx["quiet"], fx["quiet_arc"], 0), leaked],
        _now(),
        lessons=[{"ref": "lesson:leak", "projects": [fx["dominant"]], "content": "leak"}],
    )

    data = _get(client, fx["quiet"]).json()

    assert [a["ref"] for a in data["arcs"]] == [f"arc:{fx['quiet_arc']}"]
    assert data["lessons"] == []
    assert data["metrics"] == {"project_feed_cross_scope_failures": 2}


def test_a_snapshot_naming_another_project_is_served_as_missing(monkeypatch, tmp_path, fx):
    vault = tmp_path / "vault"
    client = _client(monkeypatch, vault)
    _seed(vault, fx)
    wrong = vault / "brain-feed" / "projects" / f"{_key(fx['quiet'])}.json"
    data = json.loads(wrong.read_text(encoding="utf-8"))
    data["project_id"] = fx["dominant"]
    wrong.write_text(json.dumps(data), encoding="utf-8")

    served = _get(client, fx["quiet"]).json()

    assert served["arcs"] == []
    assert served["freshness"]["state"] == "missing"
    assert served["metrics"] == {"project_feed_cross_scope_failures": 1}


def test_a_stale_intent_is_served_with_its_stale_label(monkeypatch, tmp_path, fx):
    vault = tmp_path / "vault"
    client = _client(monkeypatch, vault)
    _seed(vault, fx)
    earlier = _now() - timedelta(minutes=30)
    _intent(vault, fx["quiet"], "Finishing the cold export task.", "stale", earlier)
    _intent(vault, fx["dominant"], "Draining the queue.", "fresh", earlier)

    quiet = _get(client, fx["quiet"]).json()["intent"]
    dominant = _get(client, fx["dominant"]).json()["intent"]

    assert quiet == {
        "text": "Finishing the cold export task.",
        "label": "inferred",
        "state": "stale",
        "inferred_at": _iso(earlier),
    }
    assert dominant["state"] == "fresh"
    assert dominant["label"] == "inferred"


def test_an_old_fresh_intent_and_an_old_snapshot_read_as_stale(monkeypatch, tmp_path, fx):
    vault = tmp_path / "vault"
    client = _client(monkeypatch, vault)
    _seed(vault, fx)
    old = _now() - timedelta(days=2)
    _snapshot(vault, fx["quiet"], [_arc(fx["quiet"], fx["quiet_arc"], 0)], old)
    _intent(vault, fx["quiet"], "Finishing the cold export task.", "fresh", old)

    data = _get(client, fx["quiet"]).json()

    assert data["intent"]["state"] == "stale"
    assert data["intent"]["text"] == "Finishing the cold export task."
    assert data["freshness"]["state"] == "stale"
    assert data["freshness"]["generated_at"] == _iso(old)
    assert data["freshness"]["age_seconds"] >= 2 * 86400


def test_an_intent_naming_another_project_is_not_served(monkeypatch, tmp_path, fx):
    vault = tmp_path / "vault"
    client = _client(monkeypatch, vault)
    _seed(vault, fx)
    path = vault / "brain-feed" / "project-intent" / f"{_key(fx['quiet'])}.json"
    _intent(vault, fx["quiet"], "Draining the queue.", "fresh", _now())
    record = json.loads(path.read_text(encoding="utf-8"))
    record["project_id"] = fx["dominant"]
    path.write_text(json.dumps(record), encoding="utf-8")

    data = _get(client, fx["quiet"]).json()

    assert data["intent"] is None
    assert data["metrics"] == {"project_feed_cross_scope_failures": 1}
