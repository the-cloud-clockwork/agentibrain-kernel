"""Tick synthesis reads lesson evidence, folds paraphrased echoes and links summary sources."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_BRAIN_TOOLS = _HERE.parent
if str(_BRAIN_TOOLS) not in sys.path:
    sys.path.insert(0, str(_BRAIN_TOOLS))

brain_apply = importlib.import_module("brain_apply")
brain_tick_prompt = importlib.import_module("brain_tick_prompt")
lesson_reconcile = importlib.import_module("lesson_reconcile")
markers = importlib.import_module("markers")

LESSON = "NFS volume dirs need chmod 777 before the vault writer can create lesson files."
PARAPHRASE = "The vault writer can create lesson files only once NFS volume dirs need chmod 777."
APPLIED = "NFS volume dirs need chmod 777 so the vault writer can create lesson files."
DISTINCT = "psycopg2 connections are not thread safe; use ThreadedConnectionPool."


def _occurrence(arc: str, body: str, evidence: str = "", sessions: tuple[str, ...] = ()) -> dict:
    return {"arc": arc, "body": body, "evidence": evidence, "sessions": list(sessions)}


def _arc(root: Path, cluster_id: str, created: str, sessions: list[str], body: str) -> Path:
    path = root / "clusters" / (created[:10] or "2026-10-09") / f"{cluster_id}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["---", f"cluster_id: {cluster_id}", f"title: {cluster_id}", "heat: 5"]
    lines += ["region: left", "status: active"]
    lines += [f"created: {created}"] if created else []
    if sessions:
        lines += ["source_sessions:", *(f"  - {sid}" for sid in sessions)]
    lines += ["synthesized: false", "---", "", body, ""]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _lesson(body: str, evidence: str = "") -> str:
    attrs = f" evidence={evidence}" if evidence else ""
    return f"<!-- @lesson{attrs} -->\n{body}\n<!-- @/lesson -->"


def _section(prompt: str, heading: str, end: str) -> str:
    return prompt.split(heading, 1)[1].split(end, 1)[0]


def test_paraphrased_echo_folds_into_the_first_lesson() -> None:
    folded = lesson_reconcile.fold_lessons(
        [
            _occurrence("arc-a", LESSON, sessions=("s1",)),
            _occurrence("arc-b", PARAPHRASE, sessions=("s2",)),
            _occurrence("arc-c", DISTINCT, sessions=("s3",)),
        ]
    )
    assert [lesson["body"] for lesson in folded] == [LESSON, DISTINCT]
    assert folded[0]["arcs"] == ["arc-a"]
    assert folded[0]["sessions"] == ["s1"]
    assert folded[0]["echoes"] == [_occurrence("arc-b", PARAPHRASE, sessions=("s2",))]
    assert folded[1]["echoes"] == []


def test_paraphrase_with_its_own_evidence_adds_an_outcome() -> None:
    folded = lesson_reconcile.fold_lessons(
        [
            _occurrence("arc-a", LESSON, "toolu_a", ("s1",)),
            _occurrence("arc-b", APPLIED, "toolu_b", ("s2", "s1")),
        ]
    )
    assert len(folded) == 1
    assert folded[0]["evidence"] == ["toolu_a", "toolu_b"]
    assert folded[0]["arcs"] == ["arc-a", "arc-b"]
    assert folded[0]["sessions"] == ["s1", "s2"]
    assert folded[0]["echoes"] == []


def test_distinct_lessons_never_fold() -> None:
    near_miss = "Use ThreadedConnectionPool for psycopg2 queries."
    stopwords_only = ["It is so.", "If so, then."]
    folded = lesson_reconcile.fold_lessons(
        [
            _occurrence("arc-a", "Use ThreadedConnectionPool for psycopg2 connections."),
            _occurrence("arc-b", near_miss),
            _occurrence("arc-c", LESSON),
            _occurrence("arc-d", DISTINCT),
            *(_occurrence("arc-e", body) for body in stopwords_only),
        ]
    )
    assert len(folded) == 6
    assert all(lesson["echoes"] == [] for lesson in folded)


def test_same_arc_echo_keeps_the_evidenced_lesson(tmp_path: Path) -> None:
    body = _lesson(LESSON, "toolu_a") + "\n\n" + _lesson(LESSON)
    _arc(tmp_path, "arc-a", "2026-10-01T09:00:00+00:00", ["s1"], body)
    _arc(tmp_path, "arc-z", "", ["s9"], _lesson(PARAPHRASE, "toolu_z"))
    prompt, _ = brain_tick_prompt.build_prompt(tmp_path, tmp_path / "brain-feed")
    lessons = _section(prompt, "## Lessons", "## Unsynthesized Arcs")
    assert f"- {LESSON} (evidence 2, sessions 2, echoes 1)" in lessons
    synth = _section(prompt, "## Unsynthesized Arcs", "## Your Tasks")
    arc_a = synth.split("- id: arc-a", 1)[1].split("- id:", 1)[0]
    assert arc_a.count("lesson:") == 1


def test_synthesis_reads_evidence_and_skips_echo_only_lessons(tmp_path: Path) -> None:
    _arc(tmp_path, "arc-a", "2026-10-01T09:00:00+00:00", ["s1"], _lesson(LESSON, "toolu_a"))
    _arc(tmp_path, "arc-b", "2026-10-02T09:00:00+00:00", ["s2"], _lesson(PARAPHRASE))
    _arc(tmp_path, "arc-c", "2026-10-03T09:00:00+00:00", ["s3"], _lesson(APPLIED, "toolu_c"))
    prompt, _ = brain_tick_prompt.build_prompt(tmp_path, tmp_path / "brain-feed")
    lessons = _section(prompt, "## Lessons", "## Unsynthesized Arcs")
    assert f"- {LESSON} (evidence 2, sessions 2, echoes 1)" in lessons
    assert PARAPHRASE not in prompt
    assert APPLIED not in lessons
    synth = _section(prompt, "## Unsynthesized Arcs", "## Your Tasks")
    arc_b = synth.split("- id: arc-b", 1)[1].split("- id:", 1)[0]
    assert "lesson:" not in arc_b
    assert "- id: arc-c" in synth
    assert APPLIED in synth


def test_arc_without_source_sessions_is_not_offered(tmp_path: Path) -> None:
    _arc(tmp_path, "arc-orphan", "2026-10-01T09:00:00+00:00", [], _lesson(DISTINCT))
    _arc(tmp_path, "arc-linked", "2026-10-01T10:00:00+00:00", ["s1"], _lesson(LESSON))
    prompt, _ = brain_tick_prompt.build_prompt(tmp_path, tmp_path / "brain-feed")
    synth = _section(prompt, "## Unsynthesized Arcs", "## Your Tasks")
    assert "arc-orphan" not in synth
    assert "- id: arc-linked" in synth


def test_summary_links_its_source_sessions(tmp_path: Path) -> None:
    path = _arc(tmp_path, "arc-a", "2026-10-01T09:00:00+00:00", ["s1", "s2"], "work")
    applied = brain_apply.apply_summaries(
        tmp_path, [{"arc": "arc-a", "summary": "Vault writer fixed."}]
    )
    fm, _ = markers.parse_frontmatter(path.read_text(encoding="utf-8"))
    assert applied == 1
    assert fm["summary"] == "Vault writer fixed."
    assert fm["summary_sources"] == ["s1", "s2"]
    assert fm["synthesized"] == "true"


def test_summary_without_source_sessions_is_refused(tmp_path: Path) -> None:
    path = _arc(tmp_path, "arc-orphan", "2026-10-01T09:00:00+00:00", [], "work")
    before = path.read_text(encoding="utf-8")
    applied = brain_apply.apply_summaries(
        tmp_path, [{"arc": "arc-orphan", "summary": "Unlinked summary."}]
    )
    assert applied == 0
    assert path.read_text(encoding="utf-8") == before
