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

import brain_keeper  # noqa: E402

DAY = 86400


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


def test_old_completed_records_are_swept(tmp_path: Path):
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


def test_failures_outlive_successes(tmp_path: Path):
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


def test_dry_run_removes_nothing(tmp_path: Path):
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
