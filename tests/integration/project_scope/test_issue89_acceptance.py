"""Issue 89 acceptance: project scope from source session to delivered memory.

Two repositories, one linked worktree each, Claude and Codex sessions on both
the checkout and the worktree, and one shared fleet warning. Every case drives
the deployed scripts and HTTP services; no snapshot or index row is written by
the test itself. Each case runs on two independent fixtures.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import sys
import uuid

import pytest

from .harness import BRAIN_ID, EMBED_BEARER, ROOT, ROUTER, Brain, Project, requires_live_stack

pytestmark = requires_live_stack

MCP_APP = ROOT / "services" / "mcp" / "app"


def _attrs(project: Project, harness: str, worktree: bool) -> dict:
    folder = project.worktree if worktree else project.checkout
    return {
        "source": f"engineer-{project.key}",
        "session_id": f"{harness}-{project.word}",
        "project_id": project.project_id,
        "project": project.project_id.rsplit("/", 1)[-1],
        "repo": project.project_id.rsplit("/", 1)[-1],
        "worktree": folder.name if worktree else "",
        "cwd": str(folder),
        "branch": f"{project.key}-task" if worktree else "dev",
        "harness": harness,
    }


def _lesson(project: Project) -> str:
    return f"Lesson {project.word}: keep the {project.word} retries bounded by the lease."


def _decision(project: Project) -> str:
    return f"Decision {project.word}: the {project.word} queue keeps one claim authority."


def _post(brain: Brain, token: str, marker: dict, key: str):
    return brain.client(token).post("/marker", json=marker, headers={"X-Idempotency-Key": key})


def _outbox_entry(marker: dict, key: str) -> dict:
    return {
        "type": marker["type"],
        "content": marker["content"],
        "attrs": marker["attrs"],
        "session_id": marker["attrs"]["session_id"],
        "idempotency_key": key,
    }


def _populate(brain: Brain) -> dict:
    """Write the fixture's memory through the client paths: direct markers and outbox replay."""
    fx = brain.fixture
    alpha, beta = fx.alpha, fx.beta
    direct = [
        (
            alpha,
            {"type": "lesson", "content": _lesson(alpha), "attrs": _attrs(alpha, "codex", True)},
        ),
        (
            beta,
            {
                "type": "decision",
                "content": _decision(beta),
                "attrs": {**_attrs(beta, "codex", False), "title": f"{beta.word} claim authority"},
            },
        ),
    ]
    replayed = [
        (
            alpha,
            {
                "type": "decision",
                "content": _decision(alpha),
                "attrs": {
                    **_attrs(alpha, "claude", False),
                    "title": f"{alpha.word} claim authority",
                },
            },
        ),
        (beta, {"type": "lesson", "content": _lesson(beta), "attrs": _attrs(beta, "claude", True)}),
    ]
    ids: dict[str, str] = {}
    for project, marker in direct:
        key = uuid.uuid4().hex
        resp = _post(brain, project.token, marker, key)
        assert resp.status_code == 200, resp.text
        ids[f"{project.key}-{marker['type']}"] = resp.json()["marker_id"]
    entries = {}
    for project, marker in replayed:
        key = uuid.uuid4().hex
        entries[project.key] = _outbox_entry(marker, key)
        drained = brain.replay_outbox([entries[project.key]], project.token)
        assert drained.returncode == 0, drained.stdout + drained.stderr
        ids[f"{project.key}-{marker['type']}"] = f"mk-{key}"
    fleet = brain.client().post(
        "/marker",
        json={
            "type": "signal",
            "content": fx.fleet_warning,
            "attrs": {"severity": "warning", "source": "fleet-monitor", "scope": "fleet"},
        },
    )
    assert fleet.status_code == 200, fleet.text
    unattributed = brain.client().post(
        "/marker", json={"type": "lesson", "content": fx.unattributed_lesson, "attrs": {}}
    )
    assert unattributed.status_code == 200, unattributed.text
    return {"ids": ids, "outbox": entries}


def _feed(brain: Brain, project: Project, token: str | None = None) -> dict:
    resp = brain.client(token or project.token).get("/feed", params={"project": project.project_id})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _contents(feed: dict) -> str:
    return json.dumps({k: feed[k] for k in ("arcs", "lessons", "decisions", "signals")})


def _mcp_tools(brain: Brain, monkeypatch) -> dict:
    monkeypatch.setenv("EMBEDDINGS_URL", brain.embeddings["url"])
    monkeypatch.setenv("EMBEDDINGS_API_KEY", EMBED_BEARER)
    monkeypatch.setenv("BRAIN_API_URL", brain.url)
    monkeypatch.setenv("KB_ROUTER_TOKEN", ROUTER)
    monkeypatch.syspath_prepend(str(MCP_APP))
    found: dict = {}

    class _Capture:
        def tool(self, *args, **kwargs):
            def register(fn):
                found[fn.__name__] = fn
                return fn

            return register

    for name in ("tools.common", "tools.arcs", "tools.kb"):
        module = importlib.reload(sys.modules[name]) if name in sys.modules else None
        module = module or importlib.import_module(name)
        if hasattr(module, "register"):
            module.register(_Capture())
    return found


def _run(coro):
    return json.loads(asyncio.run(coro))


def test_two_projects_get_their_own_memory_from_checkout_and_worktree_sessions(
    brain: Brain, monkeypatch
) -> None:
    fx = brain.fixture
    bundle = brain.extract()
    sessions = {s["session_id"]: s for s in bundle["sessions"]}
    for project in fx.projects:
        for harness in ("claude", "codex"):
            session = sessions[f"{harness}-{project.word}"]
            assert session["project_id"] == project.project_id, session
            assert session["harness"] == harness, session
    assert sessions[f"codex-{fx.alpha.word}"]["worktree"].endswith("alpha-task")
    assert sessions[f"claude-{fx.beta.word}"]["worktree"].endswith("beta-task")
    assert sessions[f"claude-{fx.alpha.word}"]["worktree"] == ""

    written = _populate(brain)
    brain.tick()

    feeds = {p.key: _feed(brain, p) for p in fx.projects}
    for project, other in (fx.projects, fx.projects[::-1]):
        feed = feeds[project.key]
        text = _contents(feed)
        assert feed["project_id"] == project.project_id
        assert feed["brain_id"] == BRAIN_ID
        assert feed["arcs"], feed
        assert {a["projects"][0] for a in feed["arcs"]} == {project.project_id}
        assert [entry["content"] for entry in feed["lessons"]] == [_lesson(project)]
        assert [entry["content"] for entry in feed["decisions"]] == [_decision(project)]
        assert other.word not in text and fx.unattributed_lesson not in text
        assert feed["intent"]["label"] == "inferred"
        assert feed["intent"]["state"] == "fresh"
        assert project.project_id.rsplit("/", 1)[-1] in feed["intent"]["text"]
        assert other.project_id.rsplit("/", 1)[-1] not in feed["intent"]["text"]
        assert [w["content"] for w in feed["fleet_warnings"]] == [fx.fleet_warning]
        assert feed["metrics"]["project_feed_cross_scope_failures"] == 0

    arcs_by_harness = {
        p.key: {h for a in feeds[p.key]["arcs"] for h in _arc_harnesses(brain, a["ref"])}
        for p in fx.projects
    }
    assert arcs_by_harness == {"alpha": {"claude", "codex"}, "beta": {"claude", "codex"}}

    for project in fx.projects:
        for kind in ("lesson", "decision"):
            record = brain.client(project.token).get(
                f"/marker/{written['ids'][f'{project.key}-{kind}']}"
            )
            assert record.status_code == 200, record.text
            assert record.json()["provenance"]["project_id"] == project.project_id

    tools = _mcp_tools(brain, monkeypatch)
    for project, other in (fx.projects, fx.projects[::-1]):
        hits = brain.search(f"{project.word} {other.word}", project_id=project.project_id)
        assert hits, project.project_id
        assert {h["producer"] for h in hits} >= {"brain-arc", "brain-knowledge"}, hits
        for hit in hits:
            assert project.project_id in hit["metadata"]["projects"], hit
            assert other.word not in (hit["text_preview"] or ""), hit
        arcs = _run(tools["brain_search_arcs"](query=project.word, project=project.project_id))
        assert arcs["arcs"] and all(r["projects"] == [project.project_id] for r in arcs["arcs"]), (
            arcs
        )
        kb = _run(
            tools["kb_search"](query=f"{project.word} {other.word}", project=project.project_id)
        )
        assert kb["results"], kb
        assert all(project.project_id in r["metadata"]["projects"] for r in kb["results"]), kb
        assert all(r["source"] == "artifact" for r in kb["results"]), kb
    print("ISSUE89_RESULT", json.dumps({"case": "a", **fx.manifest}, sort_keys=True))


def _arc_harnesses(brain: Brain, ref: str) -> set[str]:
    cluster_id = ref.removeprefix("arc:")
    found = set()
    for path in brain.vault.rglob(f"{cluster_id}*.md"):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("- {") and '"harness"' in line:
                found.add(json.loads(line.strip()[2:])["harness"])
    return found


def test_scope_violations_are_refused_without_changing_state(brain: Brain, monkeypatch) -> None:
    fx = brain.fixture
    brain.extract()
    _populate(brain)
    brain.tick()
    feed_before, knowledge_before = brain.feed_files(), brain.knowledge_files()

    foreign = brain.client(fx.alpha.token).get("/feed", params={"project": fx.beta.project_id})
    assert foreign.status_code == 403
    assert fx.beta.word not in foreign.text
    unnamed = brain.client(fx.alpha.token).get("/feed")
    assert unnamed.status_code == 400
    claim = brain.client(fx.alpha.token).post(
        "/marker",
        json={
            "type": "lesson",
            "content": _lesson(fx.beta),
            "attrs": _attrs(fx.beta, "codex", False),
        },
    )
    assert claim.status_code == 403
    mention = brain.client(fx.alpha.token).post(
        "/marker",
        json={
            "type": "lesson",
            "content": f"Lesson {fx.alpha.word}: mentions {fx.beta.project_id} only in its text.",
            "attrs": {**_attrs(fx.alpha, "codex", True), "project": fx.beta.project_id},
        },
    )
    assert mention.status_code == 403
    assert brain.feed_files() == feed_before
    assert brain.knowledge_files() == knowledge_before

    unknown = brain.client().get("/feed", params={"project": "github.com/acme/never-seen"})
    assert unknown.status_code == 200
    empty = unknown.json()
    assert all(empty[k] == [] for k in ("arcs", "lessons", "decisions", "signals")), empty
    assert empty["intent"] is None

    for project in fx.projects:
        hits = brain.search(fx.unattributed_lesson, project_id=project.project_id)
        assert all(fx.unattributed_lesson not in (h["text_preview"] or "") for h in hits), hits
    unscoped = brain.search(fx.unattributed_lesson)
    assert any(fx.unattributed_lesson in (h["text_preview"] or "") for h in unscoped)

    tools = _mcp_tools(brain, monkeypatch)
    kb = _run(tools["kb_search"](query=fx.beta.project_id, project=fx.alpha.project_id))
    assert all(fx.beta.word not in r.get("preview", "") for r in kb["results"]), kb
    print("ISSUE89_RESULT", json.dumps({"case": "b", **fx.manifest}, sort_keys=True))


def test_replay_and_restart_keep_scope_without_duplicates(brain: Brain) -> None:
    fx = brain.fixture
    brain.extract()
    written = _populate(brain)
    brain.tick()
    first = {p.key: _feed(brain, p) for p in fx.projects}
    knowledge_before = brain.knowledge_files()
    rows_before = {p.key: len(brain.search(p.word, project_id=p.project_id)) for p in fx.projects}

    for project in fx.projects:
        again = brain.replay_outbox([written["outbox"][project.key]], project.token)
        assert again.returncode == 0, again.stdout + again.stderr
    brain.extract()
    brain.restart()
    brain.tick()

    assert brain.knowledge_files() == knowledge_before
    for project in fx.projects:
        feed = _feed(brain, project)
        assert feed["revision"] == first[project.key]["revision"], (feed, first[project.key])
        assert [entry["content"] for entry in feed["lessons"]] == [_lesson(project)]
        assert (
            len(brain.search(project.word, project_id=project.project_id))
            == rows_before[project.key]
        )

    legacy = brain.client().get("/feed")
    assert legacy.status_code == 200, legacy.text
    for project in fx.projects:
        record = brain.client(project.token).get(
            f"/marker/{written['ids'][f'{project.key}-lesson']}"
        )
        assert record.status_code == 200, record.text
        assert record.json()["provenance"]["project_id"] == project.project_id
        assert _feed(brain, project)["revision"] == first[project.key]["revision"]
    print("ISSUE89_RESULT", json.dumps({"case": "c", **fx.manifest}, sort_keys=True))


@pytest.fixture(autouse=True)
def _isolated_mcp_modules():
    yield
    for name in [n for n in sys.modules if n == "tools" or n.startswith("tools.")]:
        del sys.modules[name]
