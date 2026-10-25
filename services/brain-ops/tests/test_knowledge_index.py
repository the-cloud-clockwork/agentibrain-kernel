"""Every lesson entry and decision is its own scoped knowledge item.

The fixture is a long multi-project daily log with a late lesson, a superseded
decision and an unattributed historical entry, in two independent sets. The
embeddings service is replaced by an in-memory store behind the same HTTP
calls, so these tests are mocked evidence.

Run from repo root:
    pytest -q services/brain-ops/tests/test_knowledge_index.py
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib
import io
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
_BRAIN_OPS = _HERE.parent
if str(_BRAIN_OPS) not in sys.path:
    sys.path.insert(0, str(_BRAIN_OPS))

embed_arcs = importlib.import_module("embed_arcs")

FIXTURE = _HERE / "fixtures" / "sv2_brp03_knowledge.json"
SETS = json.loads(FIXTURE.read_text(encoding="utf-8"))["sets"]
PRODUCER = "brain-knowledge"


def _hash(text: str) -> str:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(normalized.strip().encode("utf-8")).hexdigest()[:16]


def _filler_marker(name: str, n: int) -> str:
    return "mk-" + hashlib.sha256(f"{name}-{n}".encode()).hexdigest()[:32]


def _provenance(**fields: str) -> str:
    pairs = " ".join(f'{k}="{v}"' for k, v in fields.items() if v)
    return f"<!-- @provenance {pairs} -->"


def _log_header(date: str) -> str:
    return (
        "---\n"
        f"id: lessons-{date}\n"
        f"title: Lessons — {date}\n"
        "type: lesson-log\n"
        f"created: {date}\n"
        "---\n"
    )


def _late_body(fx: dict) -> str:
    late = fx["late_lesson"]
    return f"{late['content']}\n<!-- @edge type=arc target={late['arc']} -->"


def _filler_body(fx: dict, n: int, revision: int = 0) -> str:
    text = fx["filler"]["text"].format(n=n)
    return text if not revision else f"{text} Revised {revision}."


def _write_vault(vault: Path, fx: dict, filler_revisions: dict[int, int] | None = None) -> None:
    revisions = filler_revisions or {}
    ref = vault / "left" / "reference"
    ref.mkdir(parents=True, exist_ok=True)
    date = fx["log_date"]
    entries = []
    projects = fx["filler"]["projects"]
    for n in range(fx["filler"]["count"]):
        project = projects[n % len(projects)]
        prov = _provenance(
            marker_id=_filler_marker(fx["name"], n),
            brain=fx["brain"],
            source_ts=f"{date}T{n % 24:02d}:00:00+00:00",
            project_id=project,
            projects=project,
        )
        entries.append(
            f"## {date}T{n % 24:02d}:00:00+00:00 — filler-{n} {prov}\n\n"
            f"{_filler_body(fx, n, revisions.get(n, 0))}\n"
        )
    late = fx["late_lesson"]
    prov = _provenance(
        marker_id=late["marker_id"],
        brain=fx["brain"],
        source_ts=late["ts"],
        project_id=late["project_id"],
        projects=late["project_id"],
        session_id=late["session_id"],
    )
    entries.append(
        f"## {late['ts']} — {late['source']} — `{late['session_id']}` {prov}\n\n{_late_body(fx)}\n"
    )
    (ref / f"lessons-{date}.md").write_text(
        _log_header(date) + "\n" + "\n".join(entries), encoding="utf-8"
    )

    hist = fx["historical"]
    (ref / f"lessons-{hist['log_date']}.md").write_text(
        _log_header(hist["log_date"])
        + f"\n## {hist['ts']} — {hist['source']}\n\n{hist['content']}\n",
        encoding="utf-8",
    )

    decisions = vault / "left" / "decisions"
    decisions.mkdir(parents=True, exist_ok=True)
    for role, adr in fx["decisions"].items():
        _write_decision(decisions, fx, adr, "superseded" if role == "superseded" else "accepted")


def _write_decision(decisions: Path, fx: dict, adr: dict, status: str) -> None:
    lines = [
        "---",
        f"id: {adr['id']}",
        f"title: {adr['title']}",
        "created: 2026-10-01T00:00:00+00:00",
        "source: architect",
        f"status: {status}",
    ]
    if adr.get("superseded_by") and status == "superseded":
        lines.append(f"superseded_by: {adr['superseded_by']}")
    lines += [
        f"marker_id: {adr['marker_id']}",
        f"brain: {fx['brain']}",
        f"project_id: {adr['project_id']}",
        f"projects: {adr['project_id']}",
        "---",
        "",
        f"# {adr['id']} — {adr['title']}",
        "",
        adr["content"],
        "",
    ]
    (decisions / adr["file"]).write_text("\n".join(lines), encoding="utf-8")


class _Response:
    def __init__(self, payload: dict):
        self._payload = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


class FakeEmbeddings:
    """The embeddings service's /embed, /metadata and /prune contract, in memory."""

    def __init__(self):
        self.rows: dict[str, dict] = {}
        self.posts: list[dict] = []
        self.fail_embed_at: int | None = None
        self._embed_calls = 0

    def urlopen(self, req, *_a, **_k):
        body = json.loads(req.data.decode("utf-8")) if req.data else {}
        path = req.full_url.rsplit("/", 1)[-1]
        self.posts.append({"path": path, "body": body})
        if path == "embed":
            if body["producer"] == PRODUCER:
                self._embed_calls += 1
            if body["producer"] == PRODUCER and self.fail_embed_at == self._embed_calls:
                raise urllib.error.URLError("planted embeddings outage")
            previous = self.rows.get(body["key"], {})
            self.rows[body["key"]] = {
                "producer": body["producer"],
                "content": body["content"],
                "metadata": body["metadata"],
                "embeds": previous.get("embeds", 0) + 1,
            }
            return _Response({"chunks_stored": 1})
        if path == "metadata":
            row = self.rows.get(body["key"])
            if row is None or row["producer"] != body["producer"]:
                raise urllib.error.HTTPError(req.full_url, 404, "missing", {}, None)
            row["metadata"] = body["metadata"]
            return _Response({"key": body["key"], "rows_updated": 1})
        if path == "prune":
            keep = set(body["keep_keys"])
            doomed = [
                k
                for k, r in self.rows.items()
                if r["producer"] == body["producer"] and k not in keep
            ]
            for key in doomed:
                del self.rows[key]
            return _Response({"deleted": len(doomed), "kept": len(keep)})
        raise AssertionError(f"unexpected call {req.full_url}")

    def knowledge(self) -> dict[str, dict]:
        return {k: r for k, r in self.rows.items() if r["producer"] == PRODUCER}

    def calls(self, path: str) -> list[dict]:
        return [
            p["body"]
            for p in self.posts
            if p["path"] == path and p["body"].get("producer", PRODUCER) == PRODUCER
        ]

    def reset_calls(self) -> None:
        self.posts.clear()
        self._embed_calls = 0


def _run(monkeypatch, vault: Path, store: FakeEmbeddings, *extra: str) -> tuple[int, dict]:
    monkeypatch.setattr(urllib.request, "urlopen", store.urlopen)
    monkeypatch.setattr(
        sys, "argv", ["embed_arcs.py", "--vault", str(vault), "--api-key", "k", *extra]
    )
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        rc = embed_arcs.main()
    text = buffer.getvalue()
    stats = json.loads(text[text.index("{\n") :])
    return rc, stats


@pytest.fixture(params=SETS, ids=[s["name"] for s in SETS])
def fx(request) -> dict:
    return request.param


@pytest.fixture()
def vault(tmp_path: Path, fx: dict) -> Path:
    root = tmp_path / "vault"
    _write_vault(root, fx)
    return root


# ── Indexing ─────────────────────────────────────────────────────────


def test_a_late_lesson_in_a_long_daily_log_is_its_own_item_under_its_project(
    monkeypatch, vault: Path, fx: dict
):
    store = FakeEmbeddings()
    log = vault / "left" / "reference" / f"lessons-{fx['log_date']}.md"
    assert len(log.read_text(encoding="utf-8")) > embed_arcs.LESSON_MAX_TEXT_CHARS

    rc, stats = _run(monkeypatch, vault, store, "--prune")

    assert rc == 0, stats
    late = fx["late_lesson"]
    item = store.knowledge()[f"lesson:{late['marker_id']}"]
    assert late["content"] in item["content"]
    meta = item["metadata"]
    assert meta["kind"] == "lesson"
    assert meta["project_id"] == late["project_id"]
    assert meta["projects"] == [late["project_id"]]
    assert meta["brain"] == fx["brain"]
    assert meta["scope"] == "project"
    assert meta["arcs"] == [late["arc"]]
    assert meta["status"] == "current"
    assert meta["revision"] == _hash(_late_body(fx))
    assert meta["source"] == {
        "path": f"left/reference/lessons-{fx['log_date']}.md",
        "entry_ts": late["ts"],
        "marker_id": late["marker_id"],
        "session_id": late["session_id"],
    }
    assert stats["knowledge_items_unindexed"] == 0


def test_every_entry_of_a_multi_project_log_keeps_its_own_project(
    monkeypatch, vault: Path, fx: dict
):
    store = FakeEmbeddings()
    rc, _ = _run(monkeypatch, vault, store)

    assert rc == 0
    path = f"left/reference/lessons-{fx['log_date']}.md"
    in_log = [r for r in store.knowledge().values() if r["metadata"]["source"]["path"] == path]
    assert len(in_log) == fx["filler"]["count"] + 1
    projects = fx["filler"]["projects"]
    for n in range(fx["filler"]["count"]):
        row = store.knowledge()[f"lesson:{_filler_marker(fx['name'], n)}"]
        assert row["metadata"]["project_id"] == projects[n % len(projects)]
        assert row["content"].strip().endswith(_filler_body(fx, n))
    assert {r["metadata"]["project_id"] for r in in_log} == {
        *projects,
        fx["late_lesson"]["project_id"],
    }


def test_a_superseded_decision_stays_inspectable_and_qualified(monkeypatch, vault: Path, fx: dict):
    store = FakeEmbeddings()
    rc, _ = _run(monkeypatch, vault, store)

    assert rc == 0
    old = fx["decisions"]["superseded"]
    new = fx["decisions"]["current"]
    old_item = store.knowledge()[f"decision:{old['marker_id']}"]
    new_item = store.knowledge()[f"decision:{new['marker_id']}"]
    assert old["content"] in old_item["content"]
    assert old_item["metadata"]["status"] == "superseded"
    assert old_item["metadata"]["superseded_by"] == old["superseded_by"]
    assert old_item["metadata"]["source"]["path"] == f"left/decisions/{old['file']}"
    assert old_item["metadata"]["project_id"] == old["project_id"]
    assert new_item["metadata"]["status"] == "current"
    assert new_item["metadata"]["superseded_by"] == ""


def test_an_unattributed_historical_entry_stays_unknown_and_is_reported(
    monkeypatch, vault: Path, fx: dict
):
    store = FakeEmbeddings()
    rc, stats = _run(monkeypatch, vault, store)

    assert rc == 0
    hist = fx["historical"]
    path = f"left/reference/lessons-{hist['log_date']}.md"
    key = f"lesson:lessons-{hist['log_date']}:{_hash(hist['content'])}"
    item = store.knowledge()[key]
    assert item["metadata"]["project_id"] == "unknown"
    assert item["metadata"]["projects"] == []
    assert item["metadata"]["scope"] == "unattributed"
    assert item["metadata"]["source"]["path"] == path
    assert stats["knowledge_unattributed"] == 1
    report = json.loads((vault / ".brain-knowledge-scope-report.json").read_text())
    assert report["knowledge_items_unindexed"] == 0
    assert report["unattributed"] == [
        {"key": key, "kind": "lesson", "path": path, "entry_ts": hist["ts"]}
    ]


def test_a_status_change_updates_metadata_without_reembedding(monkeypatch, vault: Path, fx: dict):
    store = FakeEmbeddings()
    assert _run(monkeypatch, vault, store)[0] == 0
    store.reset_calls()
    current = fx["decisions"]["current"]
    _write_decision(
        vault / "left" / "decisions",
        fx,
        {**current, "superseded_by": "ADR-9999"},
        "superseded",
    )

    rc, stats = _run(monkeypatch, vault, store)

    assert rc == 0
    key = f"decision:{current['marker_id']}"
    assert store.calls("embed") == []
    assert [b["key"] for b in store.calls("metadata")] == [key]
    row = store.knowledge()[key]
    assert row["embeds"] == 1
    assert row["metadata"]["status"] == "superseded"
    assert row["metadata"]["superseded_by"] == "ADR-9999"
    assert stats["knowledge_metadata_updated"] == 1


def test_an_unchanged_rerun_writes_nothing(monkeypatch, vault: Path):
    store = FakeEmbeddings()
    assert _run(monkeypatch, vault, store)[0] == 0
    store.reset_calls()

    rc, stats = _run(monkeypatch, vault, store)

    assert rc == 0
    assert store.calls("embed") == [] and store.calls("metadata") == []
    assert stats["knowledge_skipped_unchanged"] == stats["knowledge_scanned"]


# ── Rejection ────────────────────────────────────────────────────────


def test_content_naming_another_project_does_not_join_that_project(
    monkeypatch, vault: Path, fx: dict
):
    store = FakeEmbeddings()
    late = fx["late_lesson"]
    foreign = fx["foreign_project"].rsplit("/", 1)[-1]
    assert foreign in late["content"]

    rc, _ = _run(monkeypatch, vault, store)

    assert rc == 0
    meta = store.knowledge()[f"lesson:{late['marker_id']}"]["metadata"]
    assert fx["foreign_project"] not in meta["projects"]
    assert meta["projects"] == [late["project_id"]]


def test_an_unattributed_entry_is_never_attributed_from_its_folder_or_text(
    monkeypatch, vault: Path, fx: dict
):
    store = FakeEmbeddings()
    rc, _ = _run(monkeypatch, vault, store)

    assert rc == 0
    unknown = [r for r in store.knowledge().values() if r["metadata"]["project_id"] == "unknown"]
    assert len(unknown) == 1
    assert unknown[0]["metadata"]["projects"] == []


# ── Recovery ─────────────────────────────────────────────────────────


def test_a_failed_reindex_keeps_the_active_items_and_resumes_once(
    monkeypatch, vault: Path, fx: dict
):
    store = FakeEmbeddings()
    assert _run(monkeypatch, vault, store, "--prune")[0] == 0
    first_generation = {k: dict(r) for k, r in store.knowledge().items()}
    edited = [3, 4, 5]
    _write_vault(vault, fx, {n: 1 for n in edited})
    store.reset_calls()
    store.fail_embed_at = 2

    rc, stats = _run(monkeypatch, vault, store, "--prune")

    assert rc == 1
    assert set(store.knowledge()) == set(first_generation)
    keys = [f"lesson:{_filler_marker(fx['name'], n)}" for n in edited]
    assert store.knowledge()[keys[0]]["content"].strip().endswith(_filler_body(fx, 3, 1))
    for key in keys[1:]:
        assert store.knowledge()[key]["content"] == first_generation[key]["content"]
    assert stats["knowledge_items_unindexed"] == 2

    store.reset_calls()
    store.fail_embed_at = None
    rc, stats = _run(monkeypatch, vault, store, "--prune")

    assert rc == 0
    assert sorted(b["key"] for b in store.calls("embed")) == sorted(keys[1:])
    assert set(store.knowledge()) == set(first_generation)
    for n, key in zip(edited, keys, strict=True):
        assert store.knowledge()[key]["content"].strip().endswith(_filler_body(fx, n, 1))
        assert store.knowledge()[key]["embeds"] == 2
    assert stats["knowledge_items_unindexed"] == 0


def test_a_scan_read_error_never_prunes_the_knowledge_producer(monkeypatch, vault: Path, fx: dict):
    store = FakeEmbeddings()
    assert _run(monkeypatch, vault, store, "--prune")[0] == 0
    before = set(store.knowledge())
    store.reset_calls()
    unreadable = f"lessons-{fx['log_date']}.md"
    real_read = Path.read_text

    def flaky_read(self, *a, **k):
        if self.name == unreadable:
            raise OSError("planted NFS blip")
        return real_read(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", flaky_read)
    rc, _ = _run(monkeypatch, vault, store, "--prune")

    assert rc == 1
    assert [b for b in store.calls("prune") if b["producer"] == PRODUCER] == []
    assert set(store.knowledge()) == before


def test_a_removed_entry_is_pruned_only_after_a_clean_scan(monkeypatch, vault: Path, fx: dict):
    store = FakeEmbeddings()
    assert _run(monkeypatch, vault, store, "--prune")[0] == 0
    hist = fx["historical"]
    (vault / "left" / "reference" / f"lessons-{hist['log_date']}.md").unlink()

    rc, _ = _run(monkeypatch, vault, store, "--prune")

    assert rc == 0
    assert all(r["metadata"]["project_id"] != "unknown" for r in store.knowledge().values())
