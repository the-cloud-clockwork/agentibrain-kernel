"""SV2-BRP-01: arcs group by canonical project and explicit task provenance.

Run from repo root:
    pytest -q services/brain-ops/tests
"""

from __future__ import annotations

import copy
import json
import random
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
_BRAIN_TOOLS = _HERE.parent
if str(_BRAIN_TOOLS) not in sys.path:
    sys.path.insert(0, str(_BRAIN_TOOLS))

import cluster  # noqa: E402
import extract  # noqa: E402

FIXTURE = json.loads((_HERE / "fixtures" / "sv2_brp01_sessions.json").read_text())
NOW = datetime(2026, 10, 21, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _new_generation(monkeypatch):
    monkeypatch.delenv("BRAIN_ARC_GROUPING", raising=False)


def _sessions(suffix: str = "") -> list[dict]:
    sessions = copy.deepcopy(FIXTURE["sessions"])
    for s in sessions:
        s["session_id"] += suffix
        if s.get("task_id"):
            s["task_id"] += suffix
        if s.get("project_id"):
            s["project_id"] += suffix
    return sessions


def _by_id(session_id: str) -> dict:
    return next(s for s in _sessions() if s["session_id"] == session_id)


def _members(groups: list[list[dict]]) -> list[list[str]]:
    return sorted(sorted(s["session_id"] for s in g) for g in groups)


@pytest.mark.parametrize("suffix", ["", "-repeat"])
def test_unrelated_tasks_in_one_project_stay_separate_and_attempts_link(suffix):
    groups = cluster.group_sessions(_sessions(suffix))

    assert _members(groups) == sorted(
        [
            sorted([f"s-alpha-1{suffix}", f"s-alpha-2{suffix}"]),
            [f"s-beta-1{suffix}"],
            [f"s-legacy{suffix}"],
        ]
    )
    for g in groups:
        assert len({s.get("task_id") for s in g}) == 1


def test_task_arc_carries_canonical_project_and_provenance(tmp_path):
    groups = cluster.group_sessions(_sessions())
    alpha = next(g for g in groups if g[0].get("task_id") == "t-alpha")
    arc = cluster.build_cluster(alpha, NOW)

    assert arc["project"] == FIXTURE["project_id"]
    assert arc["task_id"] == "t-alpha"
    assert arc["grouping_basis"] == "task"

    text = cluster.write_stub(arc, tmp_path).read_text()
    assert f"project: {FIXTURE['project_id']}\n" in text
    assert "task_id: t-alpha\n" in text
    assert f"grouping_generation: {cluster.GROUPING_GENERATION}\n" in text
    assert "grouping_basis: task\n" in text
    assert (
        '  - {"session": "s-alpha-2", "branch": "alpha", '
        '"worktree": "/srv/worktrees/agentihooks/alpha-2", '
        '"harness": "claude", "installation": "swarm-hive-3", '
        '"phase": "", "attempt": 1, "replaces": ""}\n'
    ) in text


def test_legacy_session_stays_unattributed_beside_the_hottest_project(tmp_path):
    sessions = _sessions()
    for s in sessions:
        if s.get("project_id"):
            s["num_tool_calls"] = 5000
    before = copy.deepcopy(sessions)

    groups = cluster.group_sessions(sessions)
    legacy = next(g for g in groups if g[0]["session_id"] == "s-legacy")
    arc = cluster.build_cluster(legacy, NOW)
    hottest = max((cluster.build_cluster(g, NOW) for g in groups), key=lambda a: a["heat"])

    assert hottest["project"] == FIXTURE["project_id"]
    assert hottest["heat"] > arc["heat"]
    assert sessions == before
    assert legacy == [s for s in sessions if s["session_id"] == "s-legacy"]
    assert arc["project"] == cluster.UNATTRIBUTED
    assert arc["task_id"] == ""
    text = cluster.write_stub(arc, tmp_path).read_text()
    assert f"project: {cluster.UNATTRIBUTED}\n" in text
    assert "agentihooks" not in text.split("project:", 1)[1].split("\n", 1)[0]


def test_unattributed_sessions_never_merge_across_worktrees():
    a = {**_by_id("s-legacy"), "session_id": "u-1", "cwd": "/home/a/dev/agentihooks"}
    b = {**a, "session_id": "u-2", "cwd": "/home/b/dev/agentihooks"}
    same = {**a, "session_id": "u-3", "start": a["end"], "end": "2026-10-20T10:40:00+00:00"}

    assert _members(cluster.group_sessions([a, b, same])) == [["u-1", "u-3"], ["u-2"]]


def test_a_shared_task_id_without_a_project_does_not_merge_worktrees():
    a = {**_by_id("s-legacy"), "session_id": "u-1", "task_id": "t-1", "cwd": "/home/a/x"}
    b = {**a, "session_id": "u-2", "cwd": "/home/b/x"}

    groups = cluster.group_sessions([a, b])

    assert _members(groups) == [["u-1"], ["u-2"]]
    assert {cluster.build_cluster(g, NOW)["project"] for g in groups} == {cluster.UNATTRIBUTED}
    assert len({cluster.build_cluster(g, NOW)["cluster_id"] for g in groups}) == 2


def test_attributed_sessions_without_a_task_fall_back_to_the_time_window():
    base = {k: v for k, v in _by_id("s-alpha-1").items() if k != "task_id"}
    near = {
        **base,
        "session_id": "p-2",
        "start": "2026-10-20T11:00:00+00:00",
        "end": "2026-10-20T11:10:00+00:00",
    }
    far = {
        **base,
        "session_id": "p-3",
        "start": "2026-10-20T20:00:00+00:00",
        "end": "2026-10-20T20:10:00+00:00",
    }
    base["session_id"] = "p-1"

    groups = cluster.group_sessions([far, near, base])

    assert _members(groups) == [["p-1", "p-2"], ["p-3"]]
    assert [cluster.grouping_basis(g) for g in groups] == ["time-window", "single"]


def test_arc_grouping_ambiguities_counts_heuristic_merges_and_unattributed_sessions():
    base = {k: v for k, v in _by_id("s-alpha-1").items() if k != "task_id"}
    near = {
        **base,
        "session_id": "p-2",
        "start": "2026-10-20T11:00:00+00:00",
        "end": "2026-10-20T11:10:00+00:00",
    }

    attributed = [s for s in _sessions() if s.get("project_id")]

    assert cluster.arc_grouping_ambiguities(cluster.group_sessions(_sessions())) == {
        "total": 1,
        "merges": 0,
        "unattributed": 1,
    }
    assert cluster.arc_grouping_ambiguities(cluster.group_sessions([base, near])) == {
        "total": 1,
        "merges": 1,
        "unattributed": 0,
    }
    assert cluster.arc_grouping_ambiguities(cluster.group_sessions(attributed))["total"] == 0


def test_replay_in_any_order_gives_identical_arcs():
    first = [cluster.build_cluster(g, NOW) for g in cluster.group_sessions(_sessions())]
    shuffled = _sessions()
    random.Random(7).shuffle(shuffled)
    second = [cluster.build_cluster(g, NOW) for g in cluster.group_sessions(shuffled)]

    def view(arcs):
        return sorted(
            (a["cluster_id"], tuple(s["session_id"] for s in a["sessions"])) for a in arcs
        )

    assert view(first) == view(second)


def test_replaying_a_window_rewrites_the_same_stubs(tmp_path):
    def write(sessions):
        for g in cluster.group_sessions(sessions):
            cluster.write_stub(cluster.build_cluster(g, NOW), tmp_path)
        return {
            p.name: [ln for ln in p.read_text().splitlines() if not ln.startswith("created:")]
            for p in tmp_path.glob("*.md")
        }

    first = write(_sessions())
    stale = write([_by_id("s-alpha-1"), _by_id("s-beta-1")])
    replayed = write(_sessions())

    assert stale == first
    assert replayed == first
    assert len(first) == 3


def test_a_stale_window_cannot_shrink_a_newer_task_arc(tmp_path):
    full = cluster.group_sessions([_by_id("s-alpha-1"), _by_id("s-alpha-2")])[0]
    path = cluster.write_stub(cluster.build_cluster(full, NOW), tmp_path)
    stale = cluster.group_sessions([_by_id("s-alpha-1")])[0]

    assert cluster.write_stub(cluster.build_cluster(stale, NOW), tmp_path) == path
    assert cluster.recorded_sessions(path) == {"s-alpha-1", "s-alpha-2"}


def test_unambiguous_attributed_sessions_group_as_generation_one_did(monkeypatch):
    base = {k: v for k, v in _by_id("s-alpha-1").items() if k != "task_id"}
    near = {**base, "session_id": "p-2", "start": "2026-10-20T11:00:00+00:00"}
    near["end"] = "2026-10-20T11:10:00+00:00"
    far = {**base, "session_id": "p-3", "start": "2026-10-20T20:00:00+00:00"}
    far["end"] = "2026-10-20T20:10:00+00:00"
    sessions = [far, near, {**base, "session_id": "p-1"}]

    current = _members(cluster.group_sessions(sessions))
    monkeypatch.setenv("BRAIN_ARC_GROUPING", "legacy")

    assert _members(cluster.group_sessions(sessions)) == current


def test_missing_harness_and_installation_are_written_as_unknown(tmp_path):
    arc = cluster.build_cluster([_by_id("s-legacy")], NOW)

    text = cluster.write_stub(arc, tmp_path).read_text()

    assert '"harness": "unknown", "installation": "unknown"' in text


def test_task_arc_id_survives_a_later_linked_attempt():
    only_first = cluster.group_sessions([_by_id("s-alpha-1")])
    with_retry = cluster.group_sessions([_by_id("s-alpha-1"), _by_id("s-alpha-2")])

    assert (
        cluster.build_cluster(only_first[0], NOW)["cluster_id"]
        == (cluster.build_cluster(with_retry[0], NOW)["cluster_id"])
    )


def test_legacy_generation_rollback_restores_basename_grouping(monkeypatch):
    monkeypatch.setenv("BRAIN_ARC_GROUPING", "legacy")
    groups = cluster.group_sessions(_sessions())

    assert _members(groups) == [["s-alpha-1", "s-beta-1", "s-legacy"], ["s-alpha-2"]]
    assert cluster.build_cluster(groups[0], NOW)["project"] == "agentihooks"


def test_extract_leaves_canonical_project_unknown(tmp_path):
    path = tmp_path / "legacy-1.jsonl"
    path.write_text(
        json.dumps(
            {
                "type": "user",
                "cwd": "/home/someone/dev/agentihooks",
                "gitBranch": "dev",
                "timestamp": "2026-10-20T10:10:00Z",
                "message": {"role": "user", "content": "why is the statusline wrong"},
            }
        )
        + "\n"
    )
    since = datetime(2026, 10, 20, tzinfo=timezone.utc)

    summary = extract.process_session(path, since, NOW)

    assert summary["project_id"] is None
    assert summary["project"] == "agentihooks"
    assert summary["cwd"] == "/home/someone/dev/agentihooks"
    assert summary["harness"] == "claude"
