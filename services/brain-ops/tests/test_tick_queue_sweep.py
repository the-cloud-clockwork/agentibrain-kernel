"""Resolved tick records must not accumulate forever.

`requested/` drains itself, but `completed/` and `failed/` only grew. That cost
three ways: GET /tick/{job_id} linearly scans all three directories on every
status poll, the failed pile reads as an active fault to whoever looks, and
nothing reclaimed the space.

Run from repo root:
    pytest -q services/brain-ops/tests
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_BRAIN_TOOLS = _HERE.parent
if str(_BRAIN_TOOLS) not in sys.path:
    sys.path.insert(0, str(_BRAIN_TOOLS))

import pytest  # noqa: E402

import brain_keeper  # noqa: E402

DAY = 86400

@pytest.fixture()
def age_by_mtime(monkeypatch):
    """Judge records by mtime alone, so a test can fabricate an age.

    ctime is not settable — `os.utime` moves mtime but leaves ctime at now — so
    a test cannot manufacture an old record under the real rule, which takes
    the later of the two. These tests are about the retention arithmetic (the
    keep-min floor, the per-directory windows, dry-run), so they pin the clock
    and let `test_record_resolved_after_a_long_wait_is_not_swept_immediately`
    exercise the real mtime-vs-ctime rule against an actual rename.
    """
    monkeypatch.setattr(brain_keeper, "_resolution_time", lambda p: p.stat().st_mtime)



def _record(d: Path, name: str, age_days: float) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{name}.json"
    p.write_text('{"job_id": "x", "status": "done"}')
    stamp = time.time() - age_days * DAY
    os.utime(p, (stamp, stamp))
    return p


def _feed(tmp_path: Path) -> Path:
    feed = tmp_path / "brain-feed"
    (feed / "ticks" / "completed").mkdir(parents=True)
    (feed / "ticks" / "failed").mkdir(parents=True)
    (feed / "ticks" / "requested").mkdir(parents=True)
    return feed


def test_old_completed_records_are_swept(tmp_path: Path, age_by_mtime):
    feed = _feed(tmp_path)
    comp = feed / "ticks" / "completed"
    # Enough records that the keep-min floor does not shield them.
    for i in range(brain_keeper.BRAIN_TICK_QUEUE_KEEP_MIN + 5):
        _record(comp, f"old-{i}", brain_keeper.BRAIN_TICK_COMPLETED_RETAIN_DAYS + 10)

    stats = brain_keeper.sweep_tick_queue(feed)

    assert stats["completed_removed"] == 5
    assert len(list(comp.glob("*.json"))) == brain_keeper.BRAIN_TICK_QUEUE_KEEP_MIN


def test_recent_records_survive(tmp_path: Path):
    feed = _feed(tmp_path)
    comp = feed / "ticks" / "completed"
    for i in range(brain_keeper.BRAIN_TICK_QUEUE_KEEP_MIN + 5):
        _record(comp, f"fresh-{i}", 0.1)

    stats = brain_keeper.sweep_tick_queue(feed)

    assert stats["completed_removed"] == 0


def test_failures_outlive_successes(tmp_path: Path, age_by_mtime):
    """The failed pile is the diagnostic record and gets a longer window."""
    assert brain_keeper.BRAIN_TICK_FAILED_RETAIN_DAYS > brain_keeper.BRAIN_TICK_COMPLETED_RETAIN_DAYS
    feed = _feed(tmp_path)
    age = brain_keeper.BRAIN_TICK_COMPLETED_RETAIN_DAYS + 1
    keep_min = brain_keeper.BRAIN_TICK_QUEUE_KEEP_MIN
    for i in range(keep_min + 3):
        _record(feed / "ticks" / "completed", f"c-{i}", age)
        _record(feed / "ticks" / "failed", f"f-{i}", age)

    stats = brain_keeper.sweep_tick_queue(feed)

    assert stats["completed_removed"] == 3
    assert stats["failed_removed"] == 0


def test_pending_requests_are_never_touched(tmp_path: Path):
    """An old pending request means the drain is behind, not that it is stale."""
    feed = _feed(tmp_path)
    req = feed / "ticks" / "requested"
    _record(req, "ancient-but-pending", 400)

    brain_keeper.sweep_tick_queue(feed)

    assert (req / "ancient-but-pending.json").exists()


def test_dry_run_removes_nothing(tmp_path: Path, age_by_mtime):
    feed = _feed(tmp_path)
    comp = feed / "ticks" / "completed"
    for i in range(brain_keeper.BRAIN_TICK_QUEUE_KEEP_MIN + 5):
        _record(comp, f"old-{i}", brain_keeper.BRAIN_TICK_COMPLETED_RETAIN_DAYS + 10)

    stats = brain_keeper.sweep_tick_queue(feed, dry_run=True)

    assert stats["completed_removed"] == 5
    assert len(list(comp.glob("*.json"))) == brain_keeper.BRAIN_TICK_QUEUE_KEEP_MIN + 5


def test_missing_dirs_are_a_noop(tmp_path: Path):
    stats = brain_keeper.sweep_tick_queue(tmp_path / "nope")
    assert stats == {"completed_removed": 0, "failed_removed": 0, "errors": 0}


def test_record_resolved_after_a_long_wait_is_not_swept_immediately(tmp_path: Path):
    """A job that sat in the queue for weeks must not vanish on resolution.

    The drain resolves a request by `mv`-ing it out of requested/, and
    rename(2) preserves mtime — so the record inherits its enqueue time and,
    judged on mtime alone, is already past the retention cutoff the moment it
    lands. That deletes precisely the records worth keeping: the ones that took
    long enough to be interesting.
    """
    feed = _feed(tmp_path)
    req = feed / "ticks" / "requested"
    failed = feed / "ticks" / "failed"
    # Fill past the keep-min floor so the floor is not what saves it.
    for i in range(brain_keeper.BRAIN_TICK_QUEUE_KEEP_MIN):
        _record(failed, f"filler-{i}", 0.1)

    stuck = _record(req, "enqueued-long-ago", brain_keeper.BRAIN_TICK_FAILED_RETAIN_DAYS + 1)
    resolved = failed / "enqueued-long-ago.json"
    os.rename(stuck, resolved)  # exactly what the drain does

    stats = brain_keeper.sweep_tick_queue(feed)

    assert stats["failed_removed"] == 0
    assert resolved.exists(), "a just-resolved record was swept on the tick it landed"


def test_absurd_retention_does_not_abort_the_sweep(tmp_path: Path, monkeypatch):
    """timedelta overflows on a large enough day count.

    This runs as Phase 0c, before the arc scan, so an escape would take the
    whole tick down — feeds, dashboards and all — on every future run, from one
    fat-fingered env value (days confused for seconds).
    """
    feed = _feed(tmp_path)
    _record(feed / "ticks" / "failed", "r", 1)
    monkeypatch.setattr(brain_keeper, "BRAIN_TICK_FAILED_RETAIN_DAYS", 999999999)

    stats = brain_keeper.sweep_tick_queue(feed)

    assert stats["errors"] == 0
    assert (feed / "ticks" / "failed" / "r.json").exists()


def test_negative_keep_min_is_clamped(tmp_path: Path, monkeypatch, age_by_mtime):
    """A negative floor must not silently change which records are considered."""
    feed = _feed(tmp_path)
    comp = feed / "ticks" / "completed"
    for i in range(10):
        _record(comp, f"old-{i}", brain_keeper.BRAIN_TICK_COMPLETED_RETAIN_DAYS + 10)
    monkeypatch.setattr(brain_keeper, "BRAIN_TICK_QUEUE_KEEP_MIN", -5)

    stats = brain_keeper.sweep_tick_queue(feed)

    assert stats["completed_removed"] == 10
