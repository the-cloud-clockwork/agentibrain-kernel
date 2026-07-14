"""Tests for the undated-arc immortality fix.

An arc without a `created` frontmatter field used to be immortal: compute_heat
granted it no recency AND applied no decay, so a `status: active` arc froze at
heat 2 — above BRAIN_GRADUATE_HEAT (1) — while the graduation pass skipped it
outright for lacking a date. Graduation is the only drain (demotion merely moves
files already inside conscious/), so the vault could only ever grow: 183 arcs,
71 promotions and 1 demotion over 7 days.

Covered:
- resolve_created: frontmatter > cluster_id date prefix > filename prefix > mtime
- compute_heat: an old undated arc decays instead of freezing at 2
- is_arc: standing region docs (vision/connections/weekly-synthesis) are not arcs
- tick(): backfills `created`, and graduates a cold undated arc

Run from repo root:
    pytest -q services/brain-ops/tests
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_BRAIN_TOOLS = _HERE.parent
if str(_BRAIN_TOOLS) not in sys.path:
    sys.path.insert(0, str(_BRAIN_TOOLS))

import brain_keeper  # noqa: E402
import markers  # noqa: E402

NOW = datetime(2026, 7, 14, tzinfo=timezone.utc)


def _doc(name: str, fm: dict, path: Path | None = None) -> markers.DocumentMeta:
    return markers.DocumentMeta(path=path or Path(name), frontmatter=fm, body="", markers=[])


def test_resolve_created_prefers_frontmatter():
    dt, derived = brain_keeper.resolve_created(
        _doc("2026-01-01-x.md", {"created": "2026-07-01"})
    )
    assert dt == datetime(2026, 7, 1, tzinfo=timezone.utc)
    assert derived is False


def test_resolve_created_falls_back_to_cluster_id_then_filename():
    dt, derived = brain_keeper.resolve_created(
        _doc("whatever.md", {"cluster_id": "2026-06-10-qitp-ml"})
    )
    assert dt == datetime(2026, 6, 10, tzinfo=timezone.utc)
    assert derived is True

    dt, derived = brain_keeper.resolve_created(_doc("2026-05-02-job-seeker.md", {}))
    assert dt == datetime(2026, 5, 2, tzinfo=timezone.utc)
    assert derived is True


def test_resolve_created_falls_back_to_mtime(tmp_path):
    f = tmp_path / "no-date-anywhere.md"
    f.write_text("---\nstatus: active\n---\n\nbody\n")
    dt, derived = brain_keeper.resolve_created(_doc(f.name, {}, path=f))
    assert dt is not None and derived is True


def test_malformed_created_does_not_freeze_the_arc():
    # A garbage date must fall through to derivation, not silently disable decay.
    dt, derived = brain_keeper.resolve_created(
        _doc("2026-01-05-x.md", {"created": "not-a-date"})
    )
    assert dt == datetime(2026, 1, 5, tzinfo=timezone.utc)
    assert derived is True


def test_old_undated_active_arc_decays_instead_of_freezing_at_2():
    """The regression itself: status:active pinned heat at 2 forever."""
    old = (NOW - timedelta(days=90)).strftime("%Y-%m-%d")
    arc = _doc(f"{old}-stale-writer.md", {"status": "active"})
    heat = brain_keeper.compute_heat(arc, NOW)
    assert heat <= brain_keeper.BRAIN_GRADUATE_HEAT, (
        f"90-day-old undated arc still at heat {heat} — it can never graduate"
    )


def test_fresh_undated_arc_still_runs_hot():
    fresh = NOW.strftime("%Y-%m-%d")
    arc = _doc(f"{fresh}-live-work.md", {"status": "active", "source_sessions": ["s1"]})
    assert brain_keeper.compute_heat(arc, NOW) >= 5


def test_standing_region_docs_are_not_arcs():
    for name in ("vision.md", "connections.md", "weekly-synthesis.md"):
        assert brain_keeper.is_arc(_doc(name, {})) is False
    assert brain_keeper.is_arc(_doc("2026-07-10-qitp.md", {})) is True
    assert brain_keeper.is_arc(_doc("odd-name.md", {"cluster_id": "c1"})) is True


def _vault(tmp_path: Path) -> tuple[Path, Path]:
    vault = tmp_path / "vault"
    (vault / "clusters" / "2026-04-01").mkdir(parents=True)
    (vault / "left").mkdir(parents=True)
    feed = vault / "brain-feed"
    feed.mkdir(parents=True)
    return vault, feed


def test_tick_backfills_created_and_graduates_cold_undated_arc(tmp_path):
    vault, feed = _vault(tmp_path)
    stale = vault / "clusters" / "2026-04-01" / "2026-04-01-ancient-writer.md"
    stale.write_text(
        "---\ncluster_id: 2026-04-01-ancient-writer\nstatus: active\nheat: 2\n---\n\nold work\n"
    )

    stats = brain_keeper.tick(vault, feed)

    assert stats["created_backfilled"] >= 1
    assert "created: 2026-04-01" in (
        stale.read_text() if stale.exists() else (vault / "left" / stale.name).read_text()
    )
    assert stats["graduations"] >= 1, "cold undated arc must drain out of clusters/"
    assert (vault / "left" / stale.name).exists()


def test_standing_bridge_doc_is_never_relocated_or_stamped(tmp_path):
    """Removing the missing-`created` skip must not let region docs graduate.

    bridge/vision.md has no cluster_id and no region, so the graduation
    region-default ("left-hemisphere") would move a hand-authored doc into
    left/. It was only ever shielded by the skip this change removes.
    """
    vault, feed = _vault(tmp_path)
    bridge = vault / "bridge"
    bridge.mkdir(parents=True)
    vision = bridge / "vision.md"
    vision.write_text("---\ntitle: Vision\n---\n\nthe long game\n")
    import os
    ancient = (NOW - timedelta(days=400)).timestamp()
    os.utime(vision, (ancient, ancient))

    brain_keeper.tick(vault, feed)

    assert vision.exists(), "standing bridge doc was relocated by graduation"
    assert not (vault / "left" / "vision.md").exists()
    assert "created:" not in vision.read_text(), "standing doc must not be stamped"


def test_tick_is_idempotent_on_a_dated_hot_arc(tmp_path):
    vault, feed = _vault(tmp_path)
    fresh_day = NOW.strftime("%Y-%m-%d")
    hot = vault / "clusters" / "2026-04-01" / f"{fresh_day}-live.md"
    hot.write_text(
        f"---\ncluster_id: {fresh_day}-live\ncreated: {fresh_day}\nstatus: active\nheat: 5\n---\n\nlive\n"
    )

    brain_keeper.tick(vault, feed)
    assert hot.exists(), "a fresh arc must not be graduated away"
