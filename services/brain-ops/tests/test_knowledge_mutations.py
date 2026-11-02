"""SV2-ING-04: swarm write bursts share one writer and synthesis batches apply whole or not at all.

T-SV2-ING-04-A: a marker burst during a tick apply keeps every write, files are replaced
atomically, manual and scheduled ticks coalesce, prompt omissions are reported.
T-SV2-ING-04-B: a malformed synthesis response or a failing apply step mutates nothing.
T-SV2-ING-04-C: an interrupted commit rolls forward once from its journal, a newer write
wins over the replay, and a batch interrupted before its journal leaves the vault as it was.

Run from repo root:
    pytest -q services/brain-ops/tests
"""

from __future__ import annotations

import contextlib
import fcntl
import importlib
import json
import shutil
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
_BRAIN_OPS = _HERE.parent
_BRAIN_API = _BRAIN_OPS.parent / "brain-api"
for _path in (_BRAIN_OPS, _BRAIN_API):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

brain_apply = importlib.import_module("brain_apply")
brain_tick = importlib.import_module("brain_tick")
brain_tick_prompt = importlib.import_module("brain_tick_prompt")
knowledge_txn = importlib.import_module("knowledge_txn")
ops_markers = importlib.import_module("markers")
tick_cycle = importlib.import_module("tick_cycle")
api_markers = importlib.import_module("app.markers")

FIXTURE = json.loads((_HERE / "fixtures" / "sv2_ing04_burst.json").read_text(encoding="utf-8"))
DAY = FIXTURE["day"]
TS = f"{DAY}T10:00:00Z"
LESSON_LOG = Path("left") / "reference" / f"lessons-{DAY}.md"


def _arc_path(vault: Path, arc_id: str) -> Path:
    return vault / "clusters" / DAY / f"{arc_id}.md"


def _vault(root: Path) -> Path:
    """Build the fixture vault: three arcs, a lesson log, and one open burst signal."""
    vault = root / "vault"
    for arc in FIXTURE["arcs"]:
        body = arc["body"]
        if arc["id"] == "ing04-alpha":
            body = f"{body}\n\n{FIXTURE['signal_block']}"
        path = _arc_path(vault, arc["id"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"---\ncluster_id: {arc['id']}\ntitle: {arc['id']}\nheat: 5\nregion: left\n"
            f"status: active\nsynthesized: false\n---\n\n{body}\n",
            encoding="utf-8",
        )
    (vault / "brain-feed").mkdir(parents=True, exist_ok=True)
    api_markers.write_marker("lesson", "Seed lesson for the burst log", {"ts": TS}, vault)
    log = vault / LESSON_LOG
    log.write_text(log.read_text(encoding="utf-8") + f"\n{FIXTURE['signal_block']}\n")
    return vault


def _snapshot(vault: Path) -> dict[str, bytes]:
    return {
        path.relative_to(vault).as_posix(): path.read_bytes()
        for path in sorted(vault.rglob("*"))
        if path.is_file() and path.name != ".lock"
    }


def _apply(vault: Path, output: str) -> dict:
    return brain_apply.apply(vault, vault / "brain-feed", output)


def _burst(vault: Path, count: int, prefix: str) -> list[dict]:
    results: list[dict] = []
    lock = threading.Lock()

    def one(n: int) -> None:
        result = api_markers.write_marker(
            "lesson", f"{prefix} burst lesson number {n}", {"ts": TS}, vault
        )
        with lock:
            results.append(result)

    threads = [threading.Thread(target=one, args=(n,)) for n in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    return results


def _slow_lesson_reads(
    monkeypatch: pytest.MonkeyPatch, started: threading.Event
) -> Callable[[Path], str]:
    real = knowledge_txn.read_text

    def slow(path: Path) -> str:
        text = real(path)
        if path.name.startswith("lessons-"):
            started.set()
            time.sleep(0.2)
        return text

    monkeypatch.setattr(knowledge_txn, "read_text", slow)
    return real


@pytest.mark.parametrize("run", ["first", "second"])
def test_marker_burst_during_tick_apply_keeps_every_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, run: str
) -> None:
    """T-SV2-ING-04-A: concurrent marker writes and a tick apply lose nothing."""
    vault = _vault(tmp_path / run)
    started = threading.Event()
    _slow_lesson_reads(monkeypatch, started)
    outcome: dict = {}
    applier = threading.Thread(
        target=lambda: outcome.update(_apply(vault, FIXTURE["valid_output"]))
    )
    applier.start()
    assert started.wait(10)
    results = _burst(vault, FIXTURE["marker_burst"], run)
    applier.join(30)

    log = (vault / LESSON_LOG).read_text(encoding="utf-8")
    for n in range(FIXTURE["marker_burst"]):
        assert f"{run} burst lesson number {n}" in log
    assert "Seed lesson for the burst log" in log
    assert "(CLEARED: fixture resolved the burst)" in log
    assert sum(r["brain_mutation_conflicts_total"] for r in results) >= 1
    assert outcome["edges_applied"] == 1
    assert outcome["summaries_applied"] == 1
    assert "brain_mutation_conflicts_total" in outcome
    for arc in FIXTURE["arcs"]:
        fm, _ = ops_markers.parse_frontmatter(_arc_path(vault, arc["id"]).read_text())
        assert fm["cluster_id"] == arc["id"]
    alpha = _arc_path(vault, "ing04-alpha").read_text(encoding="utf-8")
    assert alpha.count("<!-- @edge type=related target=ing04-beta -->") == 1
    assert "summary: Gamma records" in _arc_path(vault, "ing04-gamma").read_text()


def test_apply_waits_for_the_marker_writer_and_counts_the_conflict(tmp_path: Path) -> None:
    """T-SV2-ING-04-A: the tick apply and marker writes share one writer lock."""
    vault = _vault(tmp_path)
    before = _arc_path(vault, "ing04-alpha").read_text(encoding="utf-8")
    outcome: dict = {}
    lock_path = vault / knowledge_txn.LOCK
    with lock_path.open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        applier = threading.Thread(
            target=lambda: outcome.update(_apply(vault, FIXTURE["valid_output"]))
        )
        applier.start()
        time.sleep(0.3)
        assert applier.is_alive()
        assert _arc_path(vault, "ing04-alpha").read_text(encoding="utf-8") == before
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    applier.join(30)
    assert outcome["brain_mutation_conflicts_total"] == 1
    assert "@edge type=related" in _arc_path(vault, "ing04-alpha").read_text()


def test_marker_append_replaces_the_log_instead_of_truncating_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T-SV2-ING-04-A: a failed marker append leaves the previous log intact."""
    vault = _vault(tmp_path)
    before = (vault / LESSON_LOG).read_bytes()
    real = api_markers.os.replace

    def refuse_log(src: object, dst: object) -> None:
        if Path(str(dst)).name.startswith("lessons-"):
            raise OSError("fixture refuses the log replace")
        real(src, dst)

    monkeypatch.setattr(api_markers.os, "replace", refuse_log)
    with pytest.raises(OSError):
        api_markers.write_marker("lesson", "A lesson that cannot land", {"ts": TS}, vault)
    assert (vault / LESSON_LOG).read_bytes() == before
    assert not list((vault / LESSON_LOG).parent.glob(".lessons-*"))


def test_manual_ticks_coalesce_and_scheduled_tick_yields_to_the_writer(tmp_path: Path) -> None:
    """T-SV2-ING-04-A: equivalent manual requests share one cycle; a busy writer defers the schedule."""
    vault = _vault(tmp_path)
    requested = vault / tick_cycle.QUEUE / "requested"
    requested.mkdir(parents=True, exist_ok=True)
    for n in range(5):
        (requested / f"manual-{n}.json").write_text(json.dumps({"job_id": f"m{n}"}))
    buckets = tick_cycle._pending(requested, vault / tick_cycle.QUEUE / "failed")
    assert [len(requests) for requests in buckets.values()] == [5]
    with (vault / tick_cycle.QUEUE / ".writer.lock").open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        assert tick_cycle.scheduled(vault, "vault") == 0
    assert len(list(requested.glob("*.json"))) == 5


def test_prompt_budget_reports_every_omitted_section(monkeypatch: pytest.MonkeyPatch) -> None:
    """T-SV2-ING-04-A: a bounded prompt names what the budget dropped."""
    monkeypatch.setattr(brain_tick_prompt, "MAX_PROMPT_CHARS", 40)
    edge_map, arc_table = "E" * 50, "A" * 50
    prompt, omitted = brain_tick_prompt._fit_budget(
        f"head {edge_map} {arc_table} {'S' * 60}", edge_map, arc_table
    )
    assert omitted == ["edge_map", "arc_table", "tail"]
    assert prompt.endswith("[prompt hard-truncated to fit context]\n")


def test_tick_report_records_prompt_omissions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T-SV2-ING-04-A: the tick report carries the omitted prompt sections."""
    vault = _vault(tmp_path)
    monkeypatch.setattr(brain_tick.brain_keeper, "tick", lambda *a, **k: {})
    monkeypatch.setattr(
        brain_tick.brain_tick_prompt,
        "build_prompt",
        lambda *a, **k: ("prompt", {"prompt_omitted": ["edge_map"]}),
    )
    report = brain_tick.run_tick(vault, vault / "brain-feed", dry_run=True)
    assert report["phases"]["prompt_gen"]["omitted"] == ["edge_map"]


@pytest.mark.parametrize(
    ("output", "reason"),
    [
        ("malformed_output", "merges"),
        ("unknown_severity_output", "apocalyptic"),
        ("sectionless_output", "no tick sections"),
    ],
)
def test_invalid_synthesis_output_mutates_nothing(tmp_path: Path, output: str, reason: str) -> None:
    """T-SV2-ING-04-B: validation fails before any arc, feed or health file changes."""
    vault = _vault(tmp_path)
    before = _snapshot(vault)
    result = _apply(vault, FIXTURE[output])
    assert any(reason in item for item in result["rejected"])
    assert _snapshot(vault) == before


def test_failing_apply_step_leaves_every_arc_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T-SV2-ING-04-B: a step failing after edges and signals were staged writes nothing."""
    vault = _vault(tmp_path)
    before = _snapshot(vault)

    def explode(*_a: object, **_k: object) -> int:
        raise RuntimeError("fixture summary step failed")

    monkeypatch.setattr(brain_apply, "apply_summaries", explode)
    with pytest.raises(RuntimeError):
        _apply(vault, FIXTURE["valid_output"])
    assert _snapshot(vault) == before


def test_tick_rejects_malformed_synthesis_and_exits_non_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T-SV2-ING-04-B: the tick reports the rejection, keeps the arcs and fails its exit code."""
    vault = _vault(tmp_path)
    arcs = {arc["id"]: _arc_path(vault, arc["id"]).read_bytes() for arc in FIXTURE["arcs"]}
    monkeypatch.setattr(brain_tick.brain_keeper, "tick", lambda *a, **k: {})
    monkeypatch.setattr(brain_tick.brain_tick_prompt, "build_prompt", lambda *a, **k: ("p", {}))
    monkeypatch.setattr(brain_tick, "call_llm", lambda *a, **k: FIXTURE["malformed_output"])
    monkeypatch.setattr(brain_tick, "CLICKHOUSE_URL", "")
    monkeypatch.setattr(brain_tick, "EVENT_BUS_REDIS_URL", "")
    report = brain_tick.run_tick(vault, vault / "brain-feed", inference_url="http://fixture")
    assert report["phases"]["apply"]["rejected"]
    assert {a: _arc_path(vault, a).read_bytes() for a in arcs} == arcs
    monkeypatch.setattr(brain_tick, "run_tick", lambda *a, **k: report)
    monkeypatch.setattr(sys, "argv", ["brain_tick.py", "--vault", str(vault), "--brain-feed", "f"])
    monkeypatch.setattr(brain_tick.net_wait, "require", lambda *a, **k: None)
    assert brain_tick.main() == 1


def _crash_on_op(monkeypatch: pytest.MonkeyPatch, crash_at: int) -> None:
    real = knowledge_txn._apply_op
    calls = {"n": 0}

    def crashing(*args: object, **kwargs: object) -> object:
        calls["n"] += 1
        if calls["n"] == crash_at:
            raise OSError("fixture killed the commit")
        return real(*args, **kwargs)

    monkeypatch.setattr(knowledge_txn, "_apply_op", crashing)


def _knowledge(vault: Path) -> dict[str, str]:
    paths = [_arc_path(vault, arc["id"]) for arc in FIXTURE["arcs"]] + [vault / LESSON_LOG]
    return {p.relative_to(vault).as_posix(): p.read_text(encoding="utf-8") for p in paths}


def test_interrupted_commit_rolls_forward_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T-SV2-ING-04-C: a commit killed midway completes from its journal exactly once."""
    vault = _vault(tmp_path / "crashed")
    reference = tmp_path / "reference" / "vault"
    shutil.copytree(vault, reference)
    _apply(reference, FIXTURE["valid_output"])
    with monkeypatch.context() as patch:
        _crash_on_op(patch, 2)
        with pytest.raises(OSError):
            _apply(vault, FIXTURE["valid_output"])
    assert (vault / knowledge_txn.JOURNAL).is_file()
    replay = knowledge_txn.recover(vault)
    assert replay["replayed"] >= 1
    assert replay["conflicts"] == 0
    assert not (vault / knowledge_txn.JOURNAL).exists()
    assert _knowledge(vault) == _knowledge(reference)
    assert knowledge_txn.recover(vault) == {"replayed": 0, "conflicts": 0, "discarded": 0}
    alpha = _arc_path(vault, "ing04-alpha").read_text(encoding="utf-8")
    assert alpha.count("<!-- @edge type=related target=ing04-beta -->") == 1


def test_next_apply_recovers_an_interrupted_commit_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T-SV2-ING-04-C: the following tick apply finishes the journal before its own batch."""
    vault = _vault(tmp_path)
    with monkeypatch.context() as patch:
        _crash_on_op(patch, 1)
        with pytest.raises(OSError):
            _apply(vault, FIXTURE["valid_output"])
    _apply(vault, "### 5. Brain Health\n7/10 — fixture follow up tick\n")
    assert not (vault / knowledge_txn.JOURNAL).exists()
    assert "@edge type=related" in _arc_path(vault, "ing04-alpha").read_text()


def test_replay_never_overwrites_a_newer_accepted_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T-SV2-ING-04-C: a target changed after staging keeps its newer content."""
    vault = _vault(tmp_path)
    with monkeypatch.context() as patch:
        _crash_on_op(patch, 1)
        with pytest.raises(OSError):
            _apply(vault, FIXTURE["valid_output"])
    gamma = _arc_path(vault, "ing04-gamma")
    newer = gamma.read_text(encoding="utf-8") + "\nNewer accepted note.\n"
    gamma.write_text(newer, encoding="utf-8")
    replay = knowledge_txn.recover(vault)
    assert replay["conflicts"] == 1
    assert gamma.read_text(encoding="utf-8") == newer
    assert "@edge type=related" in _arc_path(vault, "ing04-alpha").read_text()


def test_batch_interrupted_before_its_journal_leaves_the_previous_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T-SV2-ING-04-C: staged files without a journal are discarded on recovery."""
    vault = _vault(tmp_path)
    before = _snapshot(vault)

    def no_journal(*_a: object, **_k: object) -> None:
        raise OSError("fixture killed the journal write")

    with monkeypatch.context() as patch:
        patch.setattr(knowledge_txn, "_write_journal", no_journal)
        with pytest.raises(OSError):
            _apply(vault, FIXTURE["valid_output"])
    with contextlib.suppress(FileNotFoundError):
        assert any((vault / knowledge_txn.STAGING).iterdir())
    replay = knowledge_txn.recover(vault)
    assert replay["replayed"] == 0
    after = _snapshot(vault)
    assert after == before
    shutil.rmtree(vault)
