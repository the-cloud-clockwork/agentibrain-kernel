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

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_BRAIN_TOOLS = _HERE.parent
if str(_BRAIN_TOOLS) not in sys.path:
    sys.path.insert(0, str(_BRAIN_TOOLS))

import brain_keeper  # noqa: E402
import markers  # noqa: E402

# Real clock, not a pinned date: brain_keeper.tick() reads wall-clock time,
# so a pinned NOW rots — "fresh" arcs age past the decay threshold and the
# idempotency test starts failing. All offsets below are relative.
NOW = datetime.now(timezone.utc)


def _doc(name: str, fm: dict, path: Path | None = None) -> markers.DocumentMeta:
    return markers.DocumentMeta(path=path or Path(name), frontmatter=fm, body="", markers=[])


def test_resolve_created_prefers_frontmatter():
    dt, derived = brain_keeper.resolve_created(_doc("2026-01-01-x.md", {"created": "2026-07-01"}))
    assert dt == datetime(2026, 7, 1, tzinfo=timezone.utc)
    assert derived is False


def test_resolve_created_falls_back_to_cluster_id_then_filename():
    dt, derived = brain_keeper.resolve_created(
        _doc("whatever.md", {"cluster_id": "2026-06-10-sample-ml"})
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
    dt, derived = brain_keeper.resolve_created(_doc("2026-01-05-x.md", {"created": "not-a-date"}))
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
    assert brain_keeper.is_arc(_doc("2026-07-10-sample.md", {})) is True
    assert brain_keeper.is_arc(_doc("odd-name.md", {"cluster_id": "c1"})) is True


def test_lesson_logs_are_not_arcs():
    """A lesson log's filename embeds a date, but it is not a work arc.

    Treating it as one let graduation move it out of left/reference and cool it
    to heat 0, which is how months of lessons became unfindable. The cluster_id
    case matters too: the exemption must win over it, or a stray id readmits the
    file to the machinery that scattered it.
    """
    assert brain_keeper.is_arc(_doc("lessons-2026-07-02.md", {})) is False
    assert brain_keeper.is_arc(_doc("lessons-2026-08-11.md", {"cluster_id": "c1"})) is False
    # Not a lesson log — a real arc that merely mentions lessons in its name.
    assert brain_keeper.is_arc(_doc("2026-07-02-lessons-learned.md", {})) is True


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


def test_full_tick_is_stable_over_lesson_logs(tmp_path: Path, monkeypatch):
    """A lesson log must not ping-pong between the tick's phases.

    Promotion copies rather than moves and gated on heat alone, so a lesson log
    left a second copy in conscious/ that the reconcile phase then reclaimed as
    a stray and deleted — and promotion recreated it on the next tick, writing
    a backup every cycle and never converging. Heat stamping had the same
    shape: the tick added `heat:`, the reconcile stripped it back out.

    Verified over full ticks, not quick_refresh ones — quick_refresh skips the
    heat and promote phases, so it cannot see this class of bug at all.

    The threshold is pinned to 3 because that is what the deployment actually
    runs — the shipped default of 5 is overridden in the operator's
    environment. A lesson log computes heat exactly 3 (same-day recency, the
    only contribution its frontmatter can ever earn), and the promote check is
    `heat >= threshold`, so 3 >= 3 fired on every tick. At the code default of
    5 the arithmetic can never reach the bar, which is why a test at defaults
    passes identically with and without the gate and proves nothing.
    """
    monkeypatch.setattr(brain_keeper, "BRAIN_PROMOTE_HEAT", 3)
    vault = tmp_path / "vault"
    ref = vault / "left" / "reference"
    ref.mkdir(parents=True)
    (vault / "clusters").mkdir()
    feed = vault / "brain-feed"
    feed.mkdir()
    stamp = NOW.strftime("%Y-%m-%d")
    (ref / f"lessons-{stamp}.md").write_text(
        f"---\nid: lessons-{stamp}\ntitle: Lessons — {stamp}\n"
        f"type: lesson-log\ncreated: {stamp}\n---\n\n"
        f"## {NOW.isoformat()} — agent\n\n"
        "A lesson with enough substance to earn a slot in the feed.\n"
    )

    seen = []
    for _ in range(3):
        brain_keeper.tick(vault, feed, dry_run=False, quick_refresh=False)
        logs = sorted(
            str(p.relative_to(vault))
            for p in vault.rglob("lessons-*.md")
            if "_backups" not in p.parts
        )
        seen.append((logs, (ref / f"lessons-{stamp}.md").read_text()))

    assert seen[0] == seen[1] == seen[2]
    assert seen[0][0] == [f"left/reference/lessons-{stamp}.md"]
    assert "heat:" not in seen[0][1]


def test_signals_in_standing_docs_still_age_out(tmp_path: Path):
    """A signal in a doc with no `created` was immortal at any severity.

    `created` is only backfilled onto real arcs, so a standing region document
    left `_parent_arc_created` empty — and an empty value skips the age check
    entirely, which is the "broadcasts forever" behaviour the TTL work set out
    to kill.
    """
    vault, feed = _vault(tmp_path)
    standing = vault / "bridge" / "vision.md"
    standing.parent.mkdir(parents=True, exist_ok=True)
    standing.write_text(
        "---\ntitle: Vision\n---\n\n"
        "<!-- @signal severity=warning source=stale-source -->\n"
        "an ancient warning that should not broadcast forever\n"
        "<!-- @/signal -->\n"
    )
    old = (NOW - timedelta(days=120)).timestamp()
    os.utime(standing, (old, old))

    brain_keeper.tick(vault, feed, dry_run=False, quick_refresh=False)

    assert "an ancient warning" not in (feed / "signals.md").read_text()
