"""Tests for the lesson-log self-heal pass.

The old is_arc() bug let the tick treat `lessons-YYYY-MM-DD.md` as a work arc,
so the graduation and promote/demote steps moved those logs around the vault.
One date ended up living in two places at once, and repeated emissions of the
same lesson piled up inside a single file. reconcile_lessons puts every log
back at one canonical path per date and collapses byte-identical entries.

The property that makes it safe to run on every tick is idempotence: a second
pass over an already-clean vault must write nothing at all.

Run from repo root:
    pytest -q services/brain-ops/tests
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_BRAIN_TOOLS = _HERE.parent
if str(_BRAIN_TOOLS) not in sys.path:
    sys.path.insert(0, str(_BRAIN_TOOLS))

import lesson_reconcile  # noqa: E402

ENTRY_A = "A refused TCP connection means BUSY, not DOWN."
ENTRY_B = "An IBKR subscription reaches a running session without a restart."
ENTRY_C = "psycopg2 connections are not thread-safe."


def _log(*entries: tuple[str, str]) -> str:
    return "".join(f"## {ts}\n\n{body}\n\n" for ts, body in entries)


def _vault(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    (vault / "left" / "reference").mkdir(parents=True)
    (vault / "frontal-lobe" / "unconscious").mkdir(parents=True)
    return vault


def test_merges_duplicate_dates_without_losing_entries(tmp_path: Path):
    """The same date in two places merges to the union, not to one side."""
    vault = _vault(tmp_path)
    canonical = vault / "left" / "reference" / "lessons-2026-08-10.md"
    stray = vault / "frontal-lobe" / "unconscious" / "lessons-2026-08-10.md"
    canonical.write_text(_log(("2026-08-10T10:00:00+00:00", ENTRY_A)))
    stray.write_text(_log(("2026-08-10T12:00:00+00:00", ENTRY_B)))

    stats = lesson_reconcile.reconcile_lessons(vault)

    assert stats["strays_removed"] == 1
    assert not stray.exists()
    text = canonical.read_text()
    assert ENTRY_A in text and ENTRY_B in text


def test_collapses_repeated_entries(tmp_path: Path):
    """The same lesson emitted four times collapses to one."""
    vault = _vault(tmp_path)
    canonical = vault / "left" / "reference" / "lessons-2026-08-10.md"
    canonical.write_text(
        _log(
            ("2026-08-10T10:00:00+00:00", ENTRY_B),
            ("2026-08-10T11:00:00+00:00", ENTRY_B),
            ("2026-08-10T12:00:00+00:00", ENTRY_B),
            ("2026-08-10T13:00:00+00:00", ENTRY_C),
        )
    )

    stats = lesson_reconcile.reconcile_lessons(vault)

    assert stats["entries_deduped"] == 2
    assert canonical.read_text().count(ENTRY_B) == 1
    assert ENTRY_C in canonical.read_text()


def test_relocates_logs_out_of_other_regions(tmp_path: Path):
    """A log graduated into left/ comes back to left/reference/."""
    vault = _vault(tmp_path)
    stray = vault / "left" / "lessons-2026-07-02.md"
    stray.write_text(_log(("2026-07-02T09:00:00+00:00", ENTRY_C)))

    lesson_reconcile.reconcile_lessons(vault)

    assert not stray.exists()
    canonical = vault / "left" / "reference" / "lessons-2026-07-02.md"
    assert ENTRY_C in canonical.read_text()


def test_backfills_frontmatter(tmp_path: Path):
    """A legacy log with no frontmatter gets the canonical block."""
    vault = _vault(tmp_path)
    canonical = vault / "left" / "reference" / "lessons-2026-08-10.md"
    canonical.write_text(_log(("2026-08-10T10:00:00+00:00", ENTRY_A)))

    stats = lesson_reconcile.reconcile_lessons(vault)

    assert stats["frontmatter_added"] == 1
    text = canonical.read_text()
    assert text.startswith("---\n")
    assert "id: lessons-2026-08-10" in text
    assert "type: lesson-log" in text


def test_is_idempotent(tmp_path: Path):
    """A second pass over a clean vault writes nothing.

    This is what makes the pass safe to run every tick rather than once as a
    migration.
    """
    vault = _vault(tmp_path)
    canonical = vault / "left" / "reference" / "lessons-2026-08-10.md"
    stray = vault / "frontal-lobe" / "unconscious" / "lessons-2026-08-10.md"
    canonical.write_text(_log(("2026-08-10T10:00:00+00:00", ENTRY_A)))
    stray.write_text(_log(("2026-08-10T12:00:00+00:00", ENTRY_B)))

    lesson_reconcile.reconcile_lessons(vault)
    mtime_after_first = canonical.stat().st_mtime

    second = lesson_reconcile.reconcile_lessons(vault)

    assert second["merged"] == 0
    assert second["strays_removed"] == 0
    assert second["entries_deduped"] == 0
    assert canonical.stat().st_mtime == mtime_after_first


def test_backs_up_before_removing(tmp_path: Path):
    """Nothing is deleted without a copy landing in _backups/ first."""
    vault = _vault(tmp_path)
    (vault / "left" / "reference" / "lessons-2026-08-10.md").write_text(
        _log(("2026-08-10T10:00:00+00:00", ENTRY_A))
    )
    stray = vault / "frontal-lobe" / "unconscious" / "lessons-2026-08-10.md"
    stray.write_text(_log(("2026-08-10T12:00:00+00:00", ENTRY_B)))

    lesson_reconcile.reconcile_lessons(vault)

    backups = list((vault / "_backups" / "lesson-reconcile").rglob("*.md"))
    assert backups, "expected a backup copy before deletion"
    assert any(ENTRY_B in b.read_text() for b in backups)


def test_backup_dir_is_not_rescanned(tmp_path: Path):
    """Backups must not be walked, or deleted strays resurrect next tick."""
    vault = _vault(tmp_path)
    (vault / "left" / "reference" / "lessons-2026-08-10.md").write_text(
        _log(("2026-08-10T10:00:00+00:00", ENTRY_A))
    )
    stray = vault / "frontal-lobe" / "unconscious" / "lessons-2026-08-10.md"
    stray.write_text(_log(("2026-08-10T12:00:00+00:00", ENTRY_B)))

    lesson_reconcile.reconcile_lessons(vault)
    found = lesson_reconcile.find_logs(vault)

    assert all(
        "_backups" not in str(p) for paths in found.values() for p in paths
    )


def test_dry_run_writes_nothing(tmp_path: Path):
    vault = _vault(tmp_path)
    canonical = vault / "left" / "reference" / "lessons-2026-08-10.md"
    stray = vault / "frontal-lobe" / "unconscious" / "lessons-2026-08-10.md"
    canonical.write_text(_log(("2026-08-10T10:00:00+00:00", ENTRY_A)))
    stray.write_text(_log(("2026-08-10T12:00:00+00:00", ENTRY_B)))
    before = canonical.read_text()

    stats = lesson_reconcile.reconcile_lessons(vault, dry_run=True)

    assert stats["strays_removed"] == 1
    assert stray.exists()
    assert canonical.read_text() == before


def test_empty_vault_is_a_noop(tmp_path: Path):
    vault = _vault(tmp_path)
    stats = lesson_reconcile.reconcile_lessons(vault)
    assert stats["scanned"] == 0
    assert stats["merged"] == 0


def test_heading_inside_lesson_body_is_not_an_entry_boundary(tmp_path: Path):
    """A `## ` line inside a lesson's prose must not split the entry.

    A bare `^## ` boundary tore one entry into two, which defeated dedup and —
    worse — made the pass non-idempotent, so it rewrote the file and forced a
    re-embed on every tick forever. Entry headers always lead with an ISO
    timestamp; the boundary requires one.
    """
    vault = _vault(tmp_path)
    canonical = vault / "left" / "reference" / "lessons-2026-08-10.md"
    body = "Intro line.\n## Not a header, part of the lesson\ntail line."
    canonical.write_text(_log(("2026-08-10T10:00:00+00:00", body)))

    assert len(lesson_reconcile.split_entries(canonical.read_text())) == 1

    lesson_reconcile.reconcile_lessons(vault)
    after_first = canonical.read_text()
    stats = lesson_reconcile.reconcile_lessons(vault)

    assert stats["merged"] == 0
    assert canonical.read_text() == after_first
    assert "## Not a header, part of the lesson" in after_first


def test_refuses_to_merge_when_content_would_be_lost(tmp_path: Path):
    """A block the strict parser cannot see must stop the merge, not vanish.

    This pass deletes files, so the safety net verifies the result rather than
    trusting the parser: any block visible to a permissive read that is absent
    from the merged text aborts that date.
    """
    vault = _vault(tmp_path)
    canonical = vault / "left" / "reference" / "lessons-2026-08-10.md"
    stray = vault / "frontal-lobe" / "unconscious" / "lessons-2026-08-10.md"
    canonical.write_text(_log(("2026-08-10T10:00:00+00:00", ENTRY_A)))
    # No ISO timestamp — invisible to split_entries, visible to the safety net.
    stray.write_text("## legacy-header\n\nOrphaned but real content.\n")
    before_canonical = canonical.read_text()

    stats = lesson_reconcile.reconcile_lessons(vault)

    assert stats["skipped_unsafe"] == 1
    assert stats["strays_removed"] == 0
    assert stray.exists(), "a file whose content would be lost must not be deleted"
    assert canonical.read_text() == before_canonical
    assert "Orphaned but real content." in stray.read_text()


def test_delimiter_line_in_body_does_not_swallow_the_entry(tmp_path: Path):
    """A `---` inside a lesson body is content, not a frontmatter close.

    Parsing frontmatter by splitting on the first two `---` let a horizontal
    rule, diff marker or quoted YAML sample inside the first entry be read as
    the closing delimiter — swallowing the entry, header and all, and then
    deleting the file it came from. Entry boundaries are anchored on the
    timestamped header instead.
    """
    vault = _vault(tmp_path)
    stray = vault / "frontal-lobe" / "unconscious" / "lessons-2026-08-10.md"
    stray.write_text(
        "---\n## 2026-08-10T12:00:00+00:00 — agent\n\n"
        "BODY LINE 1\n---\nBODY LINE 2 must survive\n"
    )

    lesson_reconcile.reconcile_lessons(vault)

    text = (vault / "left" / "reference" / "lessons-2026-08-10.md").read_text()
    assert "BODY LINE 1" in text
    assert "BODY LINE 2 must survive" in text


def test_refuses_a_file_wearing_the_name_but_carrying_an_arc(tmp_path: Path):
    """A work arc saved under a lesson-log filename must not be rewritten.

    Classification is by filename, so an arc named `lessons-YYYY-MM-DD.md`
    would be reduced to bare lesson-log frontmatter — cluster_id, heat, region
    and the entire body gone in one tick.
    """
    vault = _vault(tmp_path)
    arc_path = vault / "left" / "reference" / "lessons-2026-08-10.md"
    arc = (
        "---\ncluster_id: abc123\ntitle: Real Arc\nheat: 8\n"
        "region: left-hemisphere\nstatus: active\n---\n\nIgnition prose.\n"
    )
    arc_path.write_text(arc)

    stats = lesson_reconcile.reconcile_lessons(vault)

    assert stats["skipped_unsafe"] == 1
    assert arc_path.read_text() == arc


def test_undecodable_file_does_not_wedge_the_pass(tmp_path: Path):
    """One stray non-UTF-8 byte must not take the whole tick down.

    UnicodeDecodeError is a ValueError, not an OSError. This pass runs before
    every other phase, so an escape would stop hot-arcs, signals, injects and
    dashboards updating too — on every tick, forever, since the pass that would
    clean the file up is the one crashing.
    """
    vault = _vault(tmp_path)
    (vault / "left" / "reference" / "lessons-2026-08-10.md").write_bytes(
        b"## 2026-08-10T10:00:00+00:00\n\nok\n\xff\xfe bad\n"
    )
    (vault / "left" / "reference" / "lessons-2026-08-11.md").write_text(
        _log(("2026-08-11T10:00:00+00:00", ENTRY_A))
    )

    stats = lesson_reconcile.reconcile_lessons(vault)

    assert stats["errors"] == 1
    # The healthy date still got processed.
    assert (vault / "left" / "reference" / "lessons-2026-08-11.md").read_text().startswith("---")


def test_backup_names_never_collide(tmp_path: Path):
    """Flattened backup paths can collide; a silent overwrite breaks the guarantee."""
    vault = _vault(tmp_path)
    (vault / "a-b").mkdir()
    (vault / "a" / "b").mkdir(parents=True)
    (vault / "left" / "reference" / "lessons-2026-08-10.md").write_text(
        _log(("2026-08-10T09:00:00+00:00", "keep"))
    )
    (vault / "a-b" / "lessons-2026-08-10.md").write_text(
        _log(("2026-08-10T10:00:00+00:00", "first stray"))
    )
    (vault / "a" / "b" / "lessons-2026-08-10.md").write_text(
        _log(("2026-08-10T11:00:00+00:00", "second stray"))
    )

    lesson_reconcile.reconcile_lessons(vault)

    bodies = [p.read_text() for p in (vault / "_backups").rglob("*") if p.is_file()]
    assert any("first stray" in b for b in bodies)
    assert any("second stray" in b for b in bodies)


def test_concurrent_append_is_skipped_not_clobbered(tmp_path: Path, monkeypatch):
    """An append landing mid-merge must not be silently overwritten.

    brain-api appends to today's log at any moment and tick-drain runs every
    minute, so the read-then-replace window recurs often. Losing the date for
    one tick is recoverable; losing the lesson is not.
    """
    vault = _vault(tmp_path)
    canonical = vault / "left" / "reference" / "lessons-2026-08-10.md"
    stray = vault / "frontal-lobe" / "unconscious" / "lessons-2026-08-10.md"
    canonical.write_text(_log(("2026-08-10T10:00:00+00:00", ENTRY_A)))
    stray.write_text(_log(("2026-08-10T11:00:00+00:00", ENTRY_B)))

    real_backup = lesson_reconcile._backup

    def racing_backup(vault_root, path, stamp):
        # Simulate brain-api appending after the merge was computed.
        if path == canonical:
            canonical.write_text(
                canonical.read_text() + f"\n## 2026-08-10T12:00:00+00:00\n\n{ENTRY_C}\n"
            )
        return real_backup(vault_root, path, stamp)

    monkeypatch.setattr(lesson_reconcile, "_backup", racing_backup)
    lesson_reconcile.reconcile_lessons(vault)

    # Whatever happened, the concurrently-appended lesson still exists.
    assert ENTRY_C in canonical.read_text()
