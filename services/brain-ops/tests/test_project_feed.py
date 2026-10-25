"""Each project gets its own ranked feed and inferred intent, built before any fleet wide cut.

The fixture is a dominant project with many hot arcs and a quieter project
with one cold but active task, in two independent sets. These tests are mocked
evidence: they run the brain-ops code on a temporary vault.

Run from repo root:
    pytest -q services/brain-ops/tests/test_project_feed.py
"""

from __future__ import annotations

import hashlib
import importlib
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
_BRAIN_OPS = _HERE.parent
if str(_BRAIN_OPS) not in sys.path:
    sys.path.insert(0, str(_BRAIN_OPS))

brain_apply = importlib.import_module("brain_apply")
brain_keeper = importlib.import_module("brain_keeper")
brain_tick_prompt = importlib.import_module("brain_tick_prompt")
markers = importlib.import_module("markers")
project_feed = importlib.import_module("project_feed")

FIXTURE = _HERE / "fixtures" / "sv2_brp04_projects.json"
SETS = json.loads(FIXTURE.read_text(encoding="utf-8"))["sets"]
NOW = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)
CREATED = datetime.now(timezone.utc).isoformat(timespec="seconds")


def _arc(path: Path, fm: dict, body: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = "".join(f"{k}: {v}\n" for k, v in fm.items())
    path.write_text(f"---\n{lines}---\n\n# {fm['title']}\n\n{body}\n", encoding="utf-8")


def _signal(content: str, **attrs: str) -> str:
    pairs = " ".join(f"{k}={v}" for k, v in attrs.items())
    return f"<!-- @signal {pairs} -->\n{content}\n<!-- @/signal -->"


def _provenance(**fields: str) -> str:
    pairs = " ".join(f'{k}="{v}"' for k, v in fields.items())
    return f"<!-- @provenance {pairs} -->"


def _write_vault(vault: Path, fx: dict) -> None:
    day = vault / "clusters" / fx["date"]
    dom, quiet = fx["dominant"], fx["quiet"]
    for n in range(dom["arcs"]):
        body = _signal(dom["signal"], severity="warning", source="dominant") if n == 0 else ""
        _arc(
            day / f"dominant-{n:02d}.md",
            {
                "cluster_id": f"dominant-{fx['brain']}-{n:02d}",
                "title": f"Dominant work {n}",
                "region": "frontal-lobe",
                "status": "active",
                "heat": dom["heat_floor"] + n % 4,
                "project": dom["project"],
                "task_id": f"task-dominant-{n}",
                "created": CREATED,
            },
            body,
        )
    _arc(
        day / f"{quiet['arc']}.md",
        {
            "cluster_id": quiet["arc"],
            "title": "Quiet cold task",
            "region": "frontal-lobe",
            "status": "active",
            "heat": quiet["heat"],
            "project": quiet["project"],
            "task_id": quiet["task"],
            "created": CREATED,
        },
        _signal(quiet["signal"], severity="critical", source="export"),
    )
    _arc(
        day / f"fleet-{fx['brain']}.md",
        {
            "cluster_id": f"fleet-{fx['brain']}",
            "title": "Fleet notes",
            "region": "frontal-lobe",
            "status": "active",
            "heat": 2,
            "project": "unattributed",
            "task_id": "",
            "created": CREATED,
        },
        _signal(fx["fleet_warning"], severity="warning", source="fleet", scope="fleet")
        + "\n"
        + _signal(fx["unshared_warning"], severity="warning", source="private"),
    )
    ref = vault / "left" / "reference"
    ref.mkdir(parents=True, exist_ok=True)
    entries = []
    for i, (project, text) in enumerate(
        ((dom["project"], dom["lesson"]), (quiet["project"], quiet["lesson"]))
    ):
        marker = "mk-" + hashlib.sha256(f"{fx['brain']}-{i}".encode()).hexdigest()[:32]
        prov = _provenance(marker_id=marker, brain=fx["brain"], project_id=project)
        entries.append(f"## {fx['date']}T1{i}:00:00+00:00 {prov}\n\n{text}\n")
    (ref / f"lessons-{fx['date']}.md").write_text(
        f"---\nid: lessons-{fx['date']}\ntype: lesson-log\n---\n\n" + "\n".join(entries),
        encoding="utf-8",
    )
    decisions = vault / "left" / "decisions"
    decisions.mkdir(parents=True, exist_ok=True)
    (decisions / f"ADR-0001-{fx['brain']}.md").write_text(
        f"---\nid: adr-{fx['brain']}\nmarker_id: mk-{fx['brain']}-decision\n"
        f"project_id: {quiet['project']}\nbrain: {fx['brain']}\n---\n\n{quiet['decision']}\n",
        encoding="utf-8",
    )


def _arcs(vault: Path) -> list[markers.DocumentMeta]:
    return [markers.extract_all(p) for p in sorted((vault / "clusters").glob("*/*.md"))]


def _refs(snapshot: dict, kind: str) -> list[str]:
    return [item["ref"] for item in snapshot[kind]]


@pytest.fixture(params=sorted(SETS))
def fx(request) -> dict:
    return SETS[request.param]


def test_the_quiet_project_keeps_its_cold_active_task_outside_the_fleet_top_ten(tmp_path, fx):
    vault = tmp_path / "vault"
    _write_vault(vault, fx)
    arcs = _arcs(vault)
    fleet_top_ten = sorted(
        (a for a in arcs if brain_keeper.is_arc(a)),
        key=lambda a: int(a.frontmatter.get("heat", 0)),
        reverse=True,
    )[:10]
    assert fx["quiet"]["arc"] not in {a.frontmatter["cluster_id"] for a in fleet_top_ten}

    snapshots = project_feed.build_snapshots(arcs, vault, NOW)

    quiet = snapshots[fx["quiet"]["project"]]
    dominant = snapshots[fx["dominant"]["project"]]
    assert _refs(quiet, "arcs") == [f"arc:{fx['quiet']['arc']}"]
    assert quiet["arcs"][0]["task_id"] == fx["quiet"]["task"]
    assert quiet["active"] is True
    assert len(dominant["arcs"]) == project_feed.ARCS_PER_PROJECT
    assert all(ref.startswith("arc:dominant-") for ref in _refs(dominant, "arcs"))
    heats = [item["heat"] for item in dominant["arcs"]]
    assert heats == sorted(heats, reverse=True)
    assert "unattributed" not in snapshots


def test_every_item_keeps_its_own_project_from_source_to_snapshot(tmp_path, fx):
    vault = tmp_path / "vault"
    _write_vault(vault, fx)

    snapshots = project_feed.build_snapshots(_arcs(vault), vault, NOW)

    for project_id, snapshot in snapshots.items():
        assert snapshot["project_id"] == project_id
        for kind in project_feed.ITEM_KINDS:
            for item in snapshot[kind]:
                assert project_id in item["projects"], (kind, item)
    quiet = snapshots[fx["quiet"]["project"]]
    dominant = snapshots[fx["dominant"]["project"]]
    assert [i["content"] for i in quiet["lessons"]] == [fx["quiet"]["lesson"]]
    assert [i["content"] for i in quiet["decisions"]] == [fx["quiet"]["decision"]]
    assert [i["content"] for i in quiet["signals"]] == [fx["quiet"]["signal"]]
    assert [i["content"] for i in dominant["lessons"]] == [fx["dominant"]["lesson"]]
    assert dominant["decisions"] == []
    assert [i["content"] for i in dominant["signals"]] == [fx["dominant"]["signal"]]
    for snapshot in snapshots.values():
        contents = json.dumps(snapshot)
        assert fx["fleet_warning"] not in contents
        assert fx["unshared_warning"] not in contents


def test_only_explicitly_shared_warnings_reach_the_fleet_record(tmp_path, fx):
    vault = tmp_path / "vault"
    _write_vault(vault, fx)

    fleet = project_feed.fleet_warnings(_arcs(vault), NOW)

    assert [w["content"] for w in fleet["warnings"]] == [fx["fleet_warning"]]
    assert fleet["warnings"][0]["scope"] == "fleet"
    assert fleet["warnings"][0]["ref"].startswith("signal:")


def test_snapshots_are_written_per_project_with_a_stable_revision(tmp_path, fx):
    vault = tmp_path / "vault"
    feed = vault / "brain-feed"
    _write_vault(vault, fx)
    arcs = _arcs(vault)

    first = project_feed.write_snapshots(
        feed, project_feed.build_snapshots(arcs, vault, NOW), project_feed.fleet_warnings(arcs, NOW)
    )
    quiet_path = feed / "projects" / f"{project_feed.project_key(fx['quiet']['project'])}.json"
    stored = json.loads(quiet_path.read_text(encoding="utf-8"))
    later = NOW + timedelta(hours=2)
    second = project_feed.write_snapshots(
        feed,
        project_feed.build_snapshots(arcs, vault, later),
        project_feed.fleet_warnings(arcs, later),
    )
    restored = json.loads(quiet_path.read_text(encoding="utf-8"))

    assert first["projects_written"] == 2
    assert second["projects_written"] == 0
    assert second["projects_unchanged"] == 2
    assert stored["project_id"] == fx["quiet"]["project"]
    assert stored["generated_at"] == NOW.isoformat(timespec="seconds")
    assert restored == stored
    assert stored["revision"] == restored["revision"]
    assert len(stored["revision"]) == 16
    assert (feed / "fleet-warnings.json").is_file()
    assert sorted(p.name for p in (feed / "projects").glob("*.json")) == sorted(
        f"{project_feed.project_key(fx[k]['project'])}.json" for k in ("dominant", "quiet")
    )


def test_a_project_that_goes_quiet_loses_its_stale_snapshot(tmp_path, fx):
    vault = tmp_path / "vault"
    feed = vault / "brain-feed"
    _write_vault(vault, fx)
    arcs = _arcs(vault)
    project_feed.write_snapshots(
        feed, project_feed.build_snapshots(arcs, vault, NOW), project_feed.fleet_warnings(arcs, NOW)
    )
    remaining = [a for a in arcs if a.frontmatter.get("project") != fx["quiet"]["project"]]
    (vault / "left" / "reference" / f"lessons-{fx['date']}.md").unlink()
    (vault / "left" / "decisions" / f"ADR-0001-{fx['brain']}.md").unlink()

    stats = project_feed.write_snapshots(
        feed,
        project_feed.build_snapshots(remaining, vault, NOW),
        project_feed.fleet_warnings(remaining, NOW),
    )

    quiet_path = feed / "projects" / f"{project_feed.project_key(fx['quiet']['project'])}.json"
    assert not quiet_path.exists()
    assert stats["projects_removed"] == 1


def test_the_deterministic_tick_writes_project_snapshots(tmp_path, fx):
    from agentibrain.scaffold import scaffold

    vault = tmp_path / "vault"
    scaffold(vault)
    _write_vault(vault, fx)
    feed = vault / "brain-feed"

    brain_keeper.tick(vault, feed)

    quiet = json.loads(
        (feed / "projects" / f"{project_feed.project_key(fx['quiet']['project'])}.json").read_text()
    )
    dominant = json.loads(
        (
            feed / "projects" / f"{project_feed.project_key(fx['dominant']['project'])}.json"
        ).read_text()
    )
    assert f"arc:{fx['quiet']['arc']}" in _refs(quiet, "arcs")
    assert all(fx["quiet"]["arc"] not in ref for ref in _refs(dominant, "arcs"))
    assert len(list((feed / "projects").glob("*.json"))) == 2
    assert (feed / "fleet-warnings.json").is_file()


def test_a_lesson_shared_by_two_projects_names_only_the_serving_project(tmp_path, fx):
    vault = tmp_path / "vault"
    _write_vault(vault, fx)
    dom, quiet = fx["dominant"]["project"], fx["quiet"]["project"]
    prov = _provenance(marker_id="mk-" + "b" * 32, brain=fx["brain"], projects=f"{dom},{quiet}")
    body = (
        f"Both projects share one release checklist.\n"
        f"<!-- @edge type=arc target={fx['quiet']['arc']} -->\n"
        f"<!-- @edge type=arc target=dominant-{fx['brain']}-00 -->"
    )
    log = vault / "left" / "reference" / f"lessons-{fx['date']}.md"
    log.write_text(
        log.read_text(encoding="utf-8") + f"\n## {fx['date']}T20:00:00+00:00 {prov}\n\n{body}\n",
        encoding="utf-8",
    )

    snapshots = project_feed.build_snapshots(_arcs(vault), vault, NOW)

    for project, arc in ((quiet, fx["quiet"]["arc"]), (dom, f"dominant-{fx['brain']}-00")):
        shared = [i for i in snapshots[project]["lessons"] if i["ref"] == "lesson:mk-" + "b" * 32]
        assert shared and shared[0]["projects"] == [project]
        assert shared[0]["arcs"] == [arc]


def test_a_fleet_warning_on_a_project_arc_stays_out_of_the_project_signals(tmp_path, fx):
    vault = tmp_path / "vault"
    _write_vault(vault, fx)
    quiet_arc = vault / "clusters" / fx["date"] / f"{fx['quiet']['arc']}.md"
    shared = f"{fx['brain']} shared from a project arc."
    quiet_arc.write_text(
        quiet_arc.read_text(encoding="utf-8")
        + _signal(shared, severity="warning", source="fleet", scope="fleet")
        + "\n",
        encoding="utf-8",
    )
    arcs = _arcs(vault)

    snapshots = project_feed.build_snapshots(arcs, vault, NOW)
    fleet = project_feed.fleet_warnings(arcs, NOW)

    assert [i["content"] for i in snapshots[fx["quiet"]["project"]]["signals"]] == [
        fx["quiet"]["signal"]
    ]
    assert sorted(w["content"] for w in fleet["warnings"]) == sorted([fx["fleet_warning"], shared])


def test_an_older_run_never_overwrites_or_removes_a_newer_snapshot(tmp_path, fx):
    vault = tmp_path / "vault"
    feed = vault / "brain-feed"
    _write_vault(vault, fx)
    arcs = _arcs(vault)
    later = NOW + timedelta(hours=2)
    project_feed.write_snapshots(
        feed,
        project_feed.build_snapshots(arcs, vault, later),
        project_feed.fleet_warnings(arcs, later),
    )
    quiet_path = feed / "projects" / f"{project_feed.project_key(fx['quiet']['project'])}.json"
    remaining = [a for a in arcs if a.frontmatter.get("project") != fx["quiet"]["project"]]

    stats = project_feed.write_snapshots(
        feed,
        project_feed.build_snapshots(remaining, vault, NOW),
        project_feed.fleet_warnings(remaining, NOW),
    )

    assert stats == {
        "projects_written": 0,
        "projects_unchanged": 0,
        "projects_removed": 0,
        "projects_kept_newer": 2,
    }
    assert json.loads(quiet_path.read_text())["generated_at"] == later.isoformat(timespec="seconds")
    fleet = json.loads((feed / "fleet-warnings.json").read_text())
    assert fleet["generated_at"] == later.isoformat(timespec="seconds")


def _ai_output(intents: dict[str, str]) -> str:
    lines = "\n".join(f"PROJECT_INTENT: {p} | {text}" for p, text in intents.items())
    return (
        "### 1. Missing Edges\nNo changes.\n"
        "### 4. Operator Intent\nWorking on the fixture.\n"
        '### 5. Brain Health\nHEALTH_JSON: {"score": 7, "reason": "fine"}\n'
        "### 6. Arc Summaries\nNone.\n"
        f"### 7. Project Intent\n{lines or 'None.'}\n"
    )


def _seed_snapshots(vault: Path, fx: dict) -> Path:
    feed = vault / "brain-feed"
    _write_vault(vault, fx)
    arcs = _arcs(vault)
    project_feed.write_snapshots(
        feed, project_feed.build_snapshots(arcs, vault, NOW), project_feed.fleet_warnings(arcs, NOW)
    )
    return feed


def _intent(feed: Path, project: str) -> dict:
    path = feed / "project-intent" / f"{project_feed.project_key(project)}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def test_the_tick_prompt_asks_for_each_active_project_intent(tmp_path, fx):
    from agentibrain.scaffold import scaffold

    vault = tmp_path / "vault"
    scaffold(vault)
    _write_vault(vault, fx)

    prompt, _ = brain_tick_prompt.build_prompt(vault, vault / "brain-feed")

    assert "### 7. Project Intent" in prompt
    assert f"- {fx['quiet']['project']}" in prompt
    assert f"- {fx['dominant']['project']}" in prompt
    assert "PROJECT_INTENT: <project id> | <one sentence>" in prompt
    assert "Respond ONLY with the 7 sections above." in prompt


def test_each_active_project_gets_its_own_inferred_intent(tmp_path, fx):
    vault = tmp_path / "vault"
    feed = _seed_snapshots(vault, fx)
    intents = {
        fx["quiet"]["project"]: "Finishing the cold export task.",
        fx["dominant"]["project"]: "Draining the payment queue backlog.",
    }

    result = brain_apply.apply(vault, feed, _ai_output(intents), now=NOW)

    for project, text in intents.items():
        record = _intent(feed, project)
        assert record["project_id"] == project
        assert record["text"] == text
        assert record["label"] == "inferred"
        assert record["state"] == "fresh"
        assert record["inferred_at"] == NOW.isoformat(timespec="seconds")
    assert result["project_intents"] == {"fresh": 2, "stale": 0, "skipped": 0}


def test_an_intent_for_a_project_without_a_snapshot_is_not_written(tmp_path, fx):
    vault = tmp_path / "vault"
    feed = _seed_snapshots(vault, fx)
    stranger = "github.com/elsewhere/stranger"

    result = brain_apply.apply(vault, feed, _ai_output({stranger: "Made up."}), now=NOW)

    assert not (feed / "project-intent" / f"{project_feed.project_key(stranger)}.json").exists()
    assert result["project_intents"]["skipped"] == 1


def test_a_failed_inference_keeps_the_last_intent_labelled_stale(tmp_path, fx):
    vault = tmp_path / "vault"
    feed = _seed_snapshots(vault, fx)
    quiet = fx["quiet"]["project"]
    brain_apply.apply(vault, feed, _ai_output({quiet: "Finishing the cold export task."}), now=NOW)
    later = NOW + timedelta(hours=2)

    result = brain_apply.apply(vault, feed, _ai_output({}), now=later)

    record = _intent(feed, quiet)
    assert record["text"] == "Finishing the cold export task."
    assert record["state"] == "stale"
    assert record["label"] == "inferred"
    assert record["inferred_at"] == NOW.isoformat(timespec="seconds")
    assert not (
        feed / "project-intent" / f"{project_feed.project_key(fx['dominant']['project'])}.json"
    ).exists()
    assert result["project_intents"] == {"fresh": 0, "stale": 1, "skipped": 0}


def test_an_older_inference_never_overwrites_a_newer_intent(tmp_path, fx):
    vault = tmp_path / "vault"
    feed = _seed_snapshots(vault, fx)
    quiet = fx["quiet"]["project"]
    later = NOW + timedelta(hours=2)
    brain_apply.apply(vault, feed, _ai_output({quiet: "Newer read."}), now=later)
    brain_apply.apply(vault, feed, _ai_output({quiet: "Newer read."}), now=later)

    result = brain_apply.apply(vault, feed, _ai_output({quiet: "Older read."}), now=NOW)

    record = _intent(feed, quiet)
    assert record["text"] == "Newer read."
    assert record["state"] == "fresh"
    assert record["inferred_at"] == later.isoformat(timespec="seconds")
    assert result["project_intents"] == {"fresh": 0, "stale": 0, "skipped": 1}
