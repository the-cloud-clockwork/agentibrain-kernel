"""SV2-ING-03: recalled lessons echoed across transcripts are not new independent memory.

T-SV2-ING-03-A: fifty echoes give one lesson and one independent outcome; origin kinds.
T-SV2-ING-03-B: echoes never promote, quoted operator lines, rejected echoes, quoting
markers, evidence before injection, quarantined arcs.
T-SV2-ING-03-C: a new outcome keeps provenance; promotion off keeps classification.

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

extract = importlib.import_module("extract")
lesson_reconcile = importlib.import_module("lesson_reconcile")
brain_tick_prompt = importlib.import_module("brain_tick_prompt")

FIXTURE = json.loads((_HERE / "fixtures" / "sv2_ing03_transcripts.json").read_text())
SINCE = datetime(2026, 10, 1, tzinfo=timezone.utc)
UNTIL = datetime(2026, 11, 1, tzinfo=timezone.utc)
LESSON_HASH = lesson_reconcile.entry_hash(FIXTURE["lesson"])


@pytest.fixture(autouse=True)
def _promotion_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BRAIN_LESSON_PROMOTION", raising=False)


def _record(kind: str, minute: int, content: object, **extra: object) -> dict:
    return {
        "type": kind,
        "timestamp": f"2026-10-21T10:{minute:02d}:00Z",
        "cwd": FIXTURE["cwd"],
        "message": {"role": kind, "content": content},
        **extra,
    }


def _write(root: Path, session_id: str, records: list[dict]) -> Path:
    path = root / f"{session_id}.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return path


def _echo_records(injection: str) -> list[dict]:
    return [
        _record("user", 0, injection, isMeta=True),
        _record("user", 1, FIXTURE["prompt"]),
        _record("assistant", 2, [{"type": "text", "text": FIXTURE["echo_reply"]}]),
    ]


def _genuine_records(tool_use_id: str, failed: bool = False) -> list[dict]:
    reply = FIXTURE["genuine_reply"].format(tool_use_id=tool_use_id)
    return [
        _record("user", 0, FIXTURE["injection"], isMeta=True),
        _record("user", 1, FIXTURE["prompt"]),
        _record(
            "assistant",
            2,
            [
                {
                    "type": "tool_use",
                    "id": tool_use_id,
                    "name": "Bash",
                    "input": {"command": FIXTURE["genuine_tool"]},
                }
            ],
        ),
        _record(
            "user",
            3,
            [
                {
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "content": FIXTURE["genuine_result"],
                    "is_error": failed,
                }
            ],
        ),
        _record("assistant", 4, [{"type": "text", "text": reply}]),
    ]


def _author_records() -> list[dict]:
    return [
        _record("user", 0, FIXTURE["prompt"]),
        _record("assistant", 1, [{"type": "text", "text": FIXTURE["echo_reply"]}]),
    ]


def _sessions(root: Path, suffix: str, genuine: bool = True) -> list[dict]:
    root.mkdir(parents=True, exist_ok=True)
    paths = [
        _write(root, f"echo-{suffix}-{i:02d}", _echo_records(FIXTURE["injection"]))
        for i in range(FIXTURE["workers"])
    ]
    if genuine:
        paths.append(_write(root, f"genuine-{suffix}", _genuine_records(f"toolu_{suffix}")))
    return [extract.process_session(p, SINCE, UNTIL) for p in paths]


@pytest.mark.parametrize("suffix", ["first", "second"])
def test_fifty_echoes_give_one_lesson_and_one_independent_outcome(
    tmp_path: Path, suffix: str
) -> None:
    sessions = _sessions(tmp_path / suffix, suffix)
    evidence = lesson_reconcile.lesson_evidence(sessions)
    assert list(evidence["lessons"]) == [LESSON_HASH]
    record = evidence["lessons"][LESSON_HASH]
    assert record["independent"] == 1
    assert record["sources"] == [f"genuine-{suffix}"]
    assert record["outcomes"] == [
        {"session_id": f"genuine-{suffix}", "evidence": f"toolu_{suffix}"}
    ]
    assert record["echoes"] == FIXTURE["workers"]
    assert evidence["memory_echo_suppression_total"] == FIXTURE["workers"]
    assert lesson_reconcile.promotable(record)
    assert record["classification"] == "corroborated"


def test_echo_sessions_carry_origin_kinds(tmp_path: Path) -> None:
    session = _sessions(tmp_path, "kinds", genuine=False)[0]
    assert session["origin_counts"] == {"user": 1, "assistant": 1, "tool": 0, "injected": 1}
    assert session["lesson_claims"] == [
        {"hash": LESSON_HASH, "origin_kind": "assistant", "echo": True, "evidence": ""}
    ]
    genuine = extract.process_session(
        _write(tmp_path, "genuine-kinds", _genuine_records("toolu_k")), SINCE, UNTIL
    )
    assert genuine["origin_counts"] == {"user": 1, "assistant": 2, "tool": 1, "injected": 1}
    assert genuine["lesson_claims"] == [
        {"hash": LESSON_HASH, "origin_kind": "assistant", "echo": False, "evidence": "toolu_k"}
    ]


def test_echoes_alone_are_never_promoted(tmp_path: Path) -> None:
    evidence = lesson_reconcile.lesson_evidence(_sessions(tmp_path, "only", genuine=False))
    record = evidence["lessons"][LESSON_HASH]
    assert record["independent"] == 0
    assert record["outcomes"] == []
    assert record["classification"] == "echo-only"
    assert not lesson_reconcile.promotable(record)


def test_quoted_operator_instruction_is_not_a_fresh_directive(tmp_path: Path) -> None:
    path = _write(tmp_path, "quoted", _echo_records(FIXTURE["operator_quote"]))
    session = extract.process_session(path, SINCE, UNTIL)
    assert session["first_user_prompt"] == FIXTURE["prompt"]
    assert session["last_user_prompt"] == FIXTURE["prompt"]
    assert "OPERATOR" not in session["first_user_prompt"] + session["last_user_prompt"]
    assert session["origin_counts"]["injected"] == 1


def test_rejected_echo_leaves_prior_evidence_untouched(tmp_path: Path) -> None:
    author = extract.process_session(_write(tmp_path, "author", _author_records()), SINCE, UNTIL)
    prior = lesson_reconcile.lesson_evidence([author])["lessons"]
    snapshot = copy.deepcopy(prior)
    echo = extract.process_session(
        _write(tmp_path, "echo", _echo_records(FIXTURE["injection"])), SINCE, UNTIL
    )
    failed = extract.process_session(
        _write(tmp_path, "failed", _genuine_records("toolu_fail", failed=True)), SINCE, UNTIL
    )
    merged = lesson_reconcile.lesson_evidence([echo, failed], prior)
    assert prior == snapshot
    record = merged["lessons"][LESSON_HASH]
    assert record["sources"] == ["author"]
    assert record["outcomes"] == []
    assert record["echo_sources"] == ["echo", "failed"]
    assert not lesson_reconcile.promotable(record)


def test_markers_inside_injected_or_tool_text_are_not_claims(tmp_path: Path) -> None:
    marker = FIXTURE["echo_reply"]
    records = [
        _record("user", 0, f"<system-reminder>\n{marker}\n</system-reminder>", isMeta=True),
        _record("user", 1, FIXTURE["prompt"]),
        _record(
            "assistant",
            2,
            [{"type": "tool_use", "id": "toolu_cat", "name": "Read", "input": {}}],
        ),
        _record(
            "user",
            3,
            [{"type": "tool_result", "tool_use_id": "toolu_cat", "content": marker}],
        ),
    ]
    session = extract.process_session(_write(tmp_path, "quoting", records), SINCE, UNTIL)
    assert session["lesson_claims"] == []
    assert lesson_reconcile.lesson_evidence([session])["lessons"] == {}


def test_evidence_from_before_the_injection_does_not_verify_an_echo(tmp_path: Path) -> None:
    records = _genuine_records("toolu_early")
    records[0], records[2], records[3] = records[2], records[3], records[0]
    session = extract.process_session(_write(tmp_path, "early", records), SINCE, UNTIL)
    assert session["lesson_claims"] == [
        {"hash": LESSON_HASH, "origin_kind": "assistant", "echo": True, "evidence": ""}
    ]


def test_suffixed_injection_still_marks_the_echo(tmp_path: Path) -> None:
    injection = f"<system-reminder>\nRecalled: {FIXTURE['lesson']} (heat 9)\n</system-reminder>"
    session = extract.process_session(
        _write(tmp_path, "suffixed", _echo_records(injection)), SINCE, UNTIL
    )
    assert session["lesson_claims"][0]["echo"] is True


def test_new_outcome_adds_evidence_without_replacing_provenance(tmp_path: Path) -> None:
    author = extract.process_session(_write(tmp_path, "author", _author_records()), SINCE, UNTIL)
    prior = lesson_reconcile.lesson_evidence([author])["lessons"]
    assert prior[LESSON_HASH]["origin"] == "author"
    assert prior[LESSON_HASH]["classification"] == "candidate"
    sessions = _sessions(tmp_path / "run", "replay")
    merged = lesson_reconcile.lesson_evidence(sessions, prior)["lessons"]
    record = merged[LESSON_HASH]
    assert record["origin"] == "author"
    assert record["sources"] == ["author", "genuine-replay"]
    assert record["independent"] == 2
    assert lesson_reconcile.promotable(record)
    replayed = lesson_reconcile.lesson_evidence(sessions, merged)["lessons"]
    assert replayed == merged
    stale = lesson_reconcile.lesson_evidence([author], merged)["lessons"]
    assert stale == merged


def test_promotion_off_keeps_candidates_classified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BRAIN_LESSON_PROMOTION", "off")
    record = lesson_reconcile.lesson_evidence(_sessions(tmp_path, "off"))["lessons"][LESSON_HASH]
    assert record["classification"] == "corroborated"
    assert not lesson_reconcile.promotable(record)


def test_quarantined_arcs_are_not_offered_for_synthesis(tmp_path: Path) -> None:
    for cid, confidence in (("arc-quarantined", "quarantined"), ("arc-task", "high")):
        path = tmp_path / "clusters" / "2026-10-21" / f"{cid}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"---\ncluster_id: {cid}\ntitle: {cid}\nheat: 5\nregion: left\n"
            f"status: active\nsynthesized: false\nconfidence: {confidence}\n---\n\nbody\n",
            encoding="utf-8",
        )
    prompt, _ = brain_tick_prompt.build_prompt(tmp_path, tmp_path / "brain-feed")
    assert "- id: arc-task" in prompt
    assert "- id: arc-quarantined" not in prompt
