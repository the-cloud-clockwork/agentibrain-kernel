"""SV2-BRP-02: every marker type keeps its scope and evidence from write to readback."""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[3]
FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "sv2_brp02_markers.json").read_text(encoding="utf-8")
)
SCOPE_KEYS = (
    "project_id",
    "project",
    "repo",
    "worktree",
    "cwd",
    "remote",
    "branch",
    "session_id",
    "swarm",
    "task",
    "task_revision",
    "lane",
    "execution",
    "attribution",
)
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


def _load(path: str, name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _scoped_client(monkeypatch, vault: Path, worker_tokens: str) -> TestClient:
    monkeypatch.setenv("KB_ROUTER_TOKENS", ROUTER)
    monkeypatch.setenv("BRAIN_ID", "hive-a")
    monkeypatch.setenv("KB_WORKER_TOKENS", worker_tokens)
    for name in _MODULES:
        if name in importlib.sys.modules:
            importlib.reload(importlib.sys.modules[name])
        else:
            importlib.import_module(name)
    from app.main import app

    return TestClient(app)


def _expected(marker: dict) -> dict:
    return {key: marker["attrs"][key] for key in SCOPE_KEYS if key in marker["attrs"]}


def _post(client: TestClient, marker: dict, token: str | None = None):
    headers = {"X-Idempotency-Key": marker["idempotency_key"]}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    attrs = {**marker["attrs"], "ts": marker["ts"]}
    return client.post(
        "/marker",
        headers=headers,
        json={"type": marker["type"], "content": marker["content"], "attrs": attrs},
    )


def _readback(client: TestClient, marker_id: str, token: str | None = None):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return client.get(f"/marker/{marker_id}", headers=headers)


def _vault_digest(vault: Path) -> dict[str, str]:
    return {
        str(path.relative_to(vault)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(vault.rglob("*"))
        if path.is_file()
    }


def _outbox_entry(marker: dict) -> dict:
    return {
        "type": marker["type"],
        "content": marker["content"],
        "attrs": marker["attrs"],
        "idempotency_key": marker["idempotency_key"],
        "session_id": marker["attrs"]["session_id"],
        "agent_name": marker["attrs"]["source"],
        "project": marker["attrs"].get("cwd", ""),
        "ts": marker["ts"],
    }


def _drain_through(client: TestClient, outbox: Path, monkeypatch) -> tuple[dict, list[dict]]:
    drain = _load("services/brain-ops/outbox_drain.py", "outbox_drain_brp02")
    replies: list[dict] = []

    def post(brain_url, token, body, idem):
        response = client.post("/marker", headers={"X-Idempotency-Key": idem}, json=body)
        response.raise_for_status()
        replies.append(response.json())

    monkeypatch.setattr(drain, "_post_marker", post)
    return drain.drain_dir(outbox, "http://brain", "token"), replies


@pytest.mark.parametrize("fixture", ["first", "second"])
def test_every_marker_type_keeps_its_scope_from_direct_write_to_readback(
    fixture, vault: Path, client
):
    for marker in FIXTURE[fixture]["markers"]:
        written = _post(client, marker)
        assert written.status_code == 201, written.text
        data = written.json()
        assert data["marker_id"].startswith("mk-")
        assert data["marker_provenance_loss_total"] == 0

        stored = (vault / data["vault_path"]).read_text(encoding="utf-8")
        assert data["marker_id"] in stored
        assert marker["attrs"]["project_id"] in stored
        assert marker["attrs"]["worktree"] in stored

        back = _readback(client, data["marker_id"])
        assert back.status_code == 200, back.text
        record = back.json()
        assert record["marker_type"] == marker["type"]
        assert record["vault_path"] == data["vault_path"]
        assert {k: record["provenance"].get(k) for k in _expected(marker)} == _expected(marker)
        assert record["provenance"]["source_ts"] == marker["ts"]


@pytest.mark.parametrize("fixture", ["first", "second"])
def test_replayed_markers_keep_scope_and_their_source_timestamp(
    fixture, vault: Path, client, tmp_path_factory, monkeypatch
):
    outbox = tmp_path_factory.mktemp(f"outbox-{fixture}")
    markers = FIXTURE[fixture]["markers"]
    for i, marker in enumerate(markers):
        (outbox / f"{i}-{marker['type']}.json").write_text(json.dumps(_outbox_entry(marker)))

    stats, replies = _drain_through(client, outbox, monkeypatch)

    assert stats["drained"] == len(markers)
    assert list(outbox.glob("*.json")) == []
    for marker, reply in zip(markers, replies, strict=True):
        record = _readback(client, reply["marker_id"]).json()
        assert {k: record["provenance"].get(k) for k in _expected(marker)} == _expected(marker)
        assert record["provenance"]["source_ts"] == marker["ts"]
        if marker["type"] == "lesson":
            assert reply["vault_path"] == f"left/reference/lessons-{marker['ts'][:10]}.md"


def test_replay_of_an_unattributed_entry_does_not_turn_its_folder_into_a_project(
    vault: Path, client, tmp_path, monkeypatch
):
    marker = FIXTURE["first"]["markers"][0]
    entry = _outbox_entry(marker)
    entry["attrs"] = {"source": "engineer@111-1", "session_id": "sess-alpha-1"}
    entry["project"] = "/home/dev/worktrees/alpha/engineer-111-1"
    (tmp_path / "outbox").mkdir()
    (tmp_path / "outbox" / "one.json").write_text(json.dumps(entry))

    _, replies = _drain_through(client, tmp_path / "outbox", monkeypatch)

    provenance = _readback(client, replies[0]["marker_id"]).json()["provenance"]
    assert "project" not in provenance
    assert provenance["cwd"] == "/home/dev/worktrees/alpha/engineer-111-1"


def test_milestone_routes_by_project_and_not_by_agent_name(vault: Path, client):
    (vault / "left" / "projects" / "alpha").mkdir(parents=True)
    (vault / "left" / "projects" / "engineer-111-1").mkdir(parents=True)
    marker = FIXTURE["first"]["markers"][2]

    data = _post(client, marker).json()

    assert data["vault_path"] == "left/projects/alpha/BLOCKS.md"
    assert not (vault / "left/projects/engineer-111-1/BLOCKS.md").exists()


def test_milestone_routes_by_the_project_id_repo_when_project_is_absent(vault: Path, client):
    (vault / "left" / "projects" / "alpha").mkdir(parents=True)
    marker = json.loads(json.dumps(FIXTURE["first"]["markers"][2]))
    del marker["attrs"]["project"]

    data = _post(client, marker).json()

    assert data["vault_path"] == "left/projects/alpha/BLOCKS.md"


def test_brain_ops_parser_reads_back_log_marker_provenance(vault: Path, client):
    parser = _load("services/brain-ops/markers.py", "brain_ops_markers_brp02")
    for marker in FIXTURE["first"]["markers"]:
        if marker["type"] not in ("lesson", "milestone"):
            continue
        data = _post(client, marker).json()
        doc = parser.extract_all(vault / data["vault_path"])
        found = [m for m in doc.markers if m.type == "provenance"]
        assert [m.attr("marker_id") for m in found] == [data["marker_id"]]
        assert found[0].attr("project_id") == marker["attrs"]["project_id"]
        assert found[0].attr("worktree") == marker["attrs"]["worktree"]


def test_lesson_dedup_still_matches_content_after_provenance_is_added(vault: Path, client):
    marker = FIXTURE["first"]["markers"][0]
    first = _post(client, marker).json()
    again = _post(client, {**marker, "idempotency_key": "c" * 32}).json()

    assert again["action"] == "duplicate"
    assert again["vault_path"] == first["vault_path"]
    record = _readback(client, again["marker_id"]).json()
    assert record["canonical_marker_id"] == first["marker_id"]
    assert record["provenance"]["project_id"] == marker["attrs"]["project_id"]


def test_unsupported_provenance_values_are_counted_as_lost(vault: Path, client):
    body = {
        "type": "lesson",
        "content": "A lesson whose worktree arrives as an object.",
        "attrs": {"project_id": "github.com/acme/alpha", "worktree": {"name": "x"}},
    }

    data = client.post("/marker", json=body).json()

    assert data["marker_provenance_loss_total"] == 1


@pytest.mark.parametrize("marker_index", [0, 1, 2, 3])
def test_a_marker_claiming_another_project_is_refused_and_writes_nothing(
    marker_index, vault: Path, monkeypatch
):
    client = _scoped_client(monkeypatch, vault, "hive-a@github.com/acme/alpha=worker-alpha")
    marker = json.loads(json.dumps(FIXTURE["second"]["markers"][marker_index]))
    marker["content"] = f"Filed for github.com/acme/alpha by alpha. {marker['content']}"
    marker["attrs"]["source"] = "alpha"
    before = _vault_digest(vault)

    refused = _post(client, marker, token="worker-alpha")

    assert refused.status_code == 403
    assert "github.com/acme/beta" not in refused.text
    assert _vault_digest(vault) == before
    denials = client.get("/denials", headers={"Authorization": f"Bearer {ROUTER}"}).json()
    assert [row["query"] for row in denials["denials"]] == ["POST /marker"]

    allowed = _post(client, FIXTURE["first"]["markers"][marker_index], token="worker-alpha")
    assert allowed.status_code == 201, allowed.text


def test_a_marker_listing_a_foreign_project_in_its_project_set_is_refused(vault: Path, monkeypatch):
    client = _scoped_client(monkeypatch, vault, "hive-a@github.com/acme/alpha=worker-alpha")
    marker = json.loads(json.dumps(FIXTURE["first"]["markers"][0]))
    marker["attrs"]["projects"] = ["github.com/acme/alpha", "github.com/acme/beta"]

    assert _post(client, marker, token="worker-alpha").status_code == 403


def test_readback_is_hidden_from_a_worker_outside_the_marker_project(vault: Path, monkeypatch):
    client = _scoped_client(
        monkeypatch,
        vault,
        "hive-a@github.com/acme/alpha=worker-alpha,hive-a@github.com/acme/beta=worker-beta",
    )
    data = _post(client, FIXTURE["second"]["markers"][0], token="worker-beta").json()

    hidden = _readback(client, data["marker_id"], token="worker-alpha")
    shown = _readback(client, data["marker_id"], token="worker-beta")

    assert hidden.status_code == 404
    assert "github.com/acme/beta" not in hidden.text
    assert shown.status_code == 200


def test_a_worker_token_without_a_project_grant_keeps_writing(vault: Path, monkeypatch):
    client = _scoped_client(monkeypatch, vault, "hive-a=worker-plain")

    assert _post(client, FIXTURE["second"]["markers"][0], token="worker-plain").status_code == 201


@pytest.mark.parametrize("marker_index", [0, 1, 2, 3])
def test_a_replay_after_restart_acknowledges_the_canonical_marker(
    marker_index, vault: Path, client
):
    import app.main as main

    marker = FIXTURE["first"]["markers"][marker_index]
    first = _post(client, marker).json()
    before = _vault_digest(vault)
    main._idempotency_cache.clear()

    stale = json.loads(json.dumps(marker))
    stale["attrs"]["task"] = "t-older"
    again = _post(client, stale).json()

    assert again["idempotent_replay"] is True
    assert again["action"] == "replayed"
    assert again["marker_id"] == first["marker_id"]
    assert again["vault_path"] == first["vault_path"]
    assert _vault_digest(vault) == before
    assert _readback(client, first["marker_id"]).json()["provenance"]["task"] == "t-alpha-1"


def _write_in_process(args: tuple[str, str, dict, str, str]) -> dict:
    import importlib as _importlib
    import sys as _sys

    _sys.path.insert(0, str(ROOT / "services" / "brain-api"))
    markers = _importlib.import_module("app.markers")
    marker_type, content, attrs, root, marker_id = args
    return markers.write_marker(marker_type, content, attrs, Path(root), marker_id=marker_id)


@pytest.mark.parametrize("marker_index", [0, 1, 2, 3])
def test_concurrent_duplicate_writes_from_separate_processes_leave_one_marker(
    marker_index, vault: Path, client
):
    import app.markers as markers

    marker = FIXTURE["first"]["markers"][marker_index]
    marker_id = markers.marker_id_for(marker["idempotency_key"])
    attrs = {**marker["attrs"], "ts": marker["ts"]}
    job = (marker["type"], marker["content"], attrs, str(vault), marker_id)

    with ProcessPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(_write_in_process, [job] * 8))

    actions = sorted(result["action"] for result in results)
    assert actions.count("replayed") == 7
    assert {result["vault_path"] for result in results} == {results[0]["vault_path"]}
    stored = (vault / results[0]["vault_path"]).read_text(encoding="utf-8")
    assert stored.count(marker_id) == 1
    if marker["type"] in ("signal", "decision"):
        folder = (vault / results[0]["vault_path"]).parent
        assert sum(marker_id in p.read_text(encoding="utf-8") for p in folder.glob("*.md")) == 1
