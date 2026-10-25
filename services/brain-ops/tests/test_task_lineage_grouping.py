"""SV2-ING-02: arcs group by task lineage rather than swarm volume.

Run from repo root:
    pytest -q services/brain-ops/tests
"""

from __future__ import annotations

import copy
import importlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
_BRAIN_TOOLS = _HERE.parent
if str(_BRAIN_TOOLS) not in sys.path:
    sys.path.insert(0, str(_BRAIN_TOOLS))

cluster = importlib.import_module("cluster")
extract = importlib.import_module("extract")
session_source = importlib.import_module("session_source")

FIXTURE = json.loads((_HERE / "fixtures" / "sv2_ing02_sessions.json").read_text())
PROJECT = FIXTURE["project_id"]
NOW = datetime(2026, 10, 21, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _new_generation(monkeypatch):
    monkeypatch.delenv("BRAIN_ARC_GROUPING", raising=False)


def _sessions(suffix: str = "") -> list[dict]:
    sessions = copy.deepcopy(FIXTURE["sessions"])
    for s in sessions:
        for key in ("session_id", "task_id", "work_thread_id", "replaces", "project_id"):
            if s.get(key):
                s[key] += suffix
    return sessions


def _by_id(session_id: str, suffix: str = "") -> dict:
    return next(s for s in _sessions(suffix) if s["session_id"] == session_id + suffix)


def _members(groups: list[list[dict]]) -> list[list[str]]:
    return sorted(sorted(s["session_id"] for s in g) for g in groups)


def _expected(suffix: str = "") -> list[list[str]]:
    groups = []
    for i in range(1, 11):
        t = f"s-t-{i:02d}"
        groups.append([f"{t}-a", f"{t}-b"])
    groups[2].append("s-t-03-resumed")
    groups[4].append("s-t-05-handoff")
    groups[6].append("s-t-07-cross")
    groups += [["s-plan-1", "s-plan-2"], ["s-tool-heavy"], ["s-unrelated"]]
    return sorted(sorted(sid + suffix for sid in g) for g in groups)


def _stub(path: Path) -> list[str]:
    return [ln for ln in path.read_text().splitlines() if not ln.startswith("created:")]


@pytest.mark.parametrize("suffix", ["", "-repeat"])
def test_twenty_agents_on_ten_tasks_give_task_arcs_not_one_bucket(suffix):
    sessions = _sessions(suffix)
    before = copy.deepcopy(sessions)
    groups = cluster.group_sessions(sessions)

    assert _members(groups) == _expected(suffix)
    assert sessions == before
    task_arcs = [g for g in groups if cluster.grouping_basis(g) == "task"]
    assert len(task_arcs) == 11
    for g in groups:
        assert len({cluster.thread_of(s) for s in g}) == 1
    assert cluster.arc_task_lineage_conflicts(sessions) == {
        "total": 1,
        "cross_thread": 1,
        "cross_project": 0,
    }


def test_handoff_and_planner_replacement_join_their_thread(tmp_path):
    groups = cluster.group_sessions(_sessions())
    t05 = next(g for g in groups if "s-t-05-handoff" in {s["session_id"] for s in g})
    plan = next(g for g in groups if "s-plan-2" in {s["session_id"] for s in g})

    assert cluster.build_cluster(t05, NOW)["task_id"] == "t-05"
    arc = cluster.build_cluster(plan, NOW)
    assert arc["task_id"] == "plan-p36"
    assert arc["grouping_basis"] == "task"
    assert arc["confidence"] == "high"

    text = cluster.write_stub(arc, tmp_path).read_text()
    provenance = [json.loads(ln[4:]) for ln in text.splitlines() if ln.startswith('  - {"session"')]
    assert [(p["session"], p["phase"], p["attempt"], p["replaces"]) for p in provenance] == [
        ("s-plan-1", "p36", 1, ""),
        ("s-plan-2", "p36", 1, "s-plan-1"),
    ]
    assert "confidence: high\n" in text


def test_cross_task_replacement_stays_in_its_own_task():
    groups = cluster.group_sessions(_sessions())
    cross = next(g for g in groups if "s-t-07-cross" in {s["session_id"] for s in g})

    assert {s["task_id"] for s in cross} == {"t-07"}
    assert all(s["session_id"] != "s-t-08-a" for s in cross)


@pytest.mark.parametrize("tools", [4000, 10**6])
def test_tool_volume_cannot_force_a_high_confidence_merge(tools):
    heavy = {**_by_id("s-tool-heavy"), "num_tool_calls": tools}
    unrelated = _by_id("s-unrelated")
    apart = cluster.group_sessions([heavy, unrelated])
    shared = {**unrelated, "worktree": heavy["worktree"], "cwd": heavy["cwd"]}
    merged = cluster.group_sessions([heavy, shared])

    assert _members(apart) == [["s-tool-heavy"], ["s-unrelated"]]
    assert [cluster.build_cluster(g, NOW)["confidence"] for g in merged] == ["quarantined"]
    assert cluster.arc_grouping_ambiguities(merged)["merges"] == 1


def test_replacement_into_another_project_is_a_conflict_not_a_link():
    other = {**_by_id("s-t-05-a"), "project_id": "github.com/acme/other"}
    handoff = _by_id("s-t-05-handoff")
    groups = cluster.group_sessions([other, handoff])

    assert _members(groups) == [["s-t-05-a"], ["s-t-05-handoff"]]
    assert cluster.thread_of(groups[1][0]) == ""
    assert cluster.arc_task_lineage_conflicts([other, handoff]) == {
        "total": 1,
        "cross_thread": 0,
        "cross_project": 1,
    }


def test_another_attempt_keeps_earlier_source_references(tmp_path):
    first = cluster.group_sessions([_by_id("s-t-03-a"), _by_id("s-t-03-b")])[0]
    path = cluster.write_stub(cluster.build_cluster(first, NOW), tmp_path)
    resumed = cluster.group_sessions([_by_id("s-t-03-resumed")])[0]

    assert cluster.write_stub(cluster.build_cluster(resumed, NOW), tmp_path) == path
    assert cluster.recorded_sessions(path) == {"s-t-03-a", "s-t-03-b", "s-t-03-resumed"}
    text = path.read_text()
    assert text.count('"session": "s-t-03-a"') == 1
    assert '"attempt": 2' in text
    assert "home-bridge://sessions/s-t-03-a" in text

    once = _stub(path)
    cluster.write_stub(cluster.build_cluster(resumed, NOW), tmp_path)
    assert _stub(path) == once
    cluster.write_stub(cluster.build_cluster(first, NOW), tmp_path)
    assert cluster.recorded_sessions(path) == {"s-t-03-a", "s-t-03-b", "s-t-03-resumed"}


def test_legacy_generation_stays_readable_beside_lineage_grouping(monkeypatch, tmp_path):
    monkeypatch.setenv("BRAIN_ARC_GROUPING", "legacy")
    legacy = cluster.group_sessions(_sessions())

    assert len(legacy) < len(_expected())
    arc = cluster.build_cluster(legacy[0], NOW)
    assert arc["grouping_generation"] == 1
    path = cluster.write_stub(arc, tmp_path)
    assert cluster.recorded_sessions(path) == {s["session_id"] for s in legacy[0]}


def test_meaningful_autonomous_work_is_admitted_with_few_prompts():
    task = _by_id("s-t-01-a")
    heavy = _by_id("s-tool-heavy")
    chatter = {**_by_id("s-unrelated"), "num_user_turns": 1, "num_tool_calls": 2}

    admitted = extract.granted_sessions([task, heavy, chatter], 5, "")

    assert [s["session_id"] for s in admitted] == ["s-t-01-a", "s-tool-heavy"]
    assert extract.granted_sessions([chatter], 1, "") == [chatter]


class _Feed:
    def __init__(self, records: list[dict]) -> None:
        self.body = records

    def changes(self, cursor, limit):
        if cursor:
            return {
                "schema_version": 1,
                "changes": [],
                "next_cursor": cursor,
                "position": 1,
                "head": 1,
            }
        change = {
            "change_id": "c1",
            "kind": "appended",
            "session_uid": "claude:hive:s1",
            "source_generation": "g1",
            "first_sequence": 1,
            "last_sequence": len(self.body),
            "harness": "claude",
        }
        return {
            "schema_version": 1,
            "changes": [change],
            "next_cursor": "1",
            "position": 1,
            "head": 1,
        }

    def records(self, change, first):
        return {
            "schema_version": 1,
            "session_uid": change["session_uid"],
            "records": self.body[first - 1 :],
        }


def _record(sequence: int, kind: str, **extra) -> dict:
    return {
        "sequence": sequence,
        "kind": kind,
        "timestamp": f"2026-10-20T10:0{sequence}:00Z",
        "project_id": PROJECT,
        **extra,
    }


def test_feed_carries_task_lineage_into_normalized_sessions(tmp_path):
    lineage = {"task_id": "t-03", "phase_id": "p36", "attempt": 2, "replaces": "claude:hive:s0"}
    records = [
        _record(1, "user", text="resume the task", lineage=lineage),
        *(_record(i, "tool_call", tool="Bash") for i in range(2, 8)),
    ]
    grant = session_source.Grant("brain-extractor@hive", frozenset({PROJECT}))
    store = session_source.Store(tmp_path / "state")

    bundle = session_source.extract_batch(_Feed(records), grant, store, min_turns=5)

    [sess] = bundle["sessions"]
    assert (sess["task_id"], sess["phase_id"], sess["attempt"], sess["replaces"]) == (
        "t-03",
        "p36",
        2,
        "claude:hive:s0",
    )
    assert sess["work_thread_id"] == ""


@pytest.mark.parametrize(
    "lineage",
    [{"task_id": 3}, {"attempt": 0}, {"attempt": "2"}, {"owner": "operator"}, "t-03"],
)
def test_malformed_lineage_refuses_the_batch_without_mutation(tmp_path, lineage):
    records = [_record(1, "user", text="resume", lineage=lineage)]
    grant = session_source.Grant("brain-extractor@hive", frozenset({PROJECT}))
    store = session_source.Store(tmp_path / "state")

    with pytest.raises(session_source.Refused):
        session_source.extract_batch(_Feed(records), grant, store, min_turns=1)
    assert not store.committed_path.exists()
