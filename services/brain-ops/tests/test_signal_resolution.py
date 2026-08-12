"""Closing an alarm by evidence, and archiving what is closed.

The incident these are written from: a deploy failed at 17:43 and fired
`nuclear`; the fix landed one minute later and a note naming the same run id
and the same commit went into the vault at 18:51. Both documents sat there
together and the alarm kept broadcasting for a day, because nothing connected
them — different ids, and the resolution was prose rather than a signal.

Run from repo root:
    pytest -q services/brain-ops/tests
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_BRAIN_OPS = _HERE.parent
if str(_BRAIN_OPS) not in sys.path:
    sys.path.insert(0, str(_BRAIN_OPS))

import brain_keeper  # noqa: E402
import markers  # noqa: E402
import signal_resolution as sr  # noqa: E402

NOW = datetime.now(timezone.utc)

ALARM = "Branch=dev SHA=6f8c941ebcb1582d055c308e67d040fbfd8410c2 Run=31518571119"
RESOLUTION = (
    "antoncore dev deploy.failed at 2026-08-11T17:43Z (run 31518571119, SHA 6f8c941e) "
    "is resolved. Cause was validate_stack running before run_secrets_stage. "
    "Fixed in cf4ad749; Build & Deploy green on 5ea1869f at 17:44Z."
)


def _sig(content: str, **attrs) -> markers.Marker:
    return markers.Marker(type="signal", attrs=dict(attrs), content=content)


def _doc(name: str, fm: dict, body: str = "", path: Path | None = None) -> markers.DocumentMeta:
    return markers.DocumentMeta(path=path or Path(name), frontmatter=fm, body=body, markers=[])


# ---------------------------------------------------------------------------
# Key extraction
# ---------------------------------------------------------------------------


def test_a_full_sha_and_its_short_form_produce_the_same_key():
    """Slugs truncate and humans quote the short SHA. If the two forms keyed
    differently, the alarm and its own resolution would never match."""
    assert "6f8c941" in sr.incident_keys(ALARM)
    assert "6f8c941" in sr.incident_keys(RESOLUTION)


def test_the_real_incident_pair_overlaps_on_run_id_and_commit():
    assert sr.incident_keys(ALARM) & sr.incident_keys(RESOLUTION) == {"3151857", "6f8c941"}


def test_words_spelled_from_hex_letters_are_not_incident_keys():
    """`defaced` is seven characters of a-f. Without the digit rule an ordinary
    word in a resolution note closes an unrelated alarm."""
    assert sr.incident_keys("defaced facade deadbee cabbage") == set()


def test_short_numbers_are_not_incident_keys():
    """A year, a port, an exit code — none of them name an incident."""
    assert sr.incident_keys("failed with code 137 on port 8103 in 2026") == set()


def test_empty_and_none_are_handled():
    assert sr.incident_keys("") == set()
    assert sr.incident_keys(None) == set()


# ---------------------------------------------------------------------------
# What counts as a resolution
# ---------------------------------------------------------------------------


def test_a_resolved_severity_signal_closes_its_incident():
    closed = sr.resolved_keys([], [_sig(RESOLUTION, severity="resolved")])
    assert sr.is_resolved(ALARM, closed)


def test_a_resolved_arc_closes_by_body_or_summary():
    arc = _doc("incident.md", {"status": "resolved", "summary": RESOLUTION})
    assert sr.is_resolved(ALARM, sr.resolved_keys([arc], []))


def test_a_graduated_arc_does_not_close_anything():
    """Graduation means an arc cooled and was filed. An alarm nobody acted on
    graduates exactly like one that was fixed, so treating graduation as
    resolution would silently close live alarms — the failure the timer
    already covers, without the timer's honesty.
    """
    arc = _doc("incident.md", {"status": "graduated", "summary": RESOLUTION})
    assert sr.resolved_keys([arc], []) == set()


def test_an_active_arc_mentioning_the_incident_does_not_close_it():
    arc = _doc("incident.md", {"status": "active", "summary": RESOLUTION})
    assert sr.resolved_keys([arc], []) == set()


def test_a_warning_note_about_the_incident_does_not_close_it():
    """The resolution in the real vault was emitted as `warning` prose. It has
    to be an explicit assertion — that is the contract this enforces."""
    assert sr.resolved_keys([], [_sig(RESOLUTION, severity="warning")]) == set()


def test_a_signal_carrying_no_identifier_can_never_be_closed_this_way():
    closed = sr.resolved_keys([], [_sig(RESOLUTION, severity="resolved")])
    assert not sr.is_resolved("disk is filling up", closed)


def test_an_unrelated_incident_is_not_closed():
    closed = sr.resolved_keys([], [_sig(RESOLUTION, severity="resolved")])
    assert not sr.is_resolved("Run=99999999999 SHA=abc1234def", closed)


# ---------------------------------------------------------------------------
# The feed
# ---------------------------------------------------------------------------


def test_a_resolved_nuclear_signal_stops_broadcasting(tmp_path: Path):
    """Evidence beats the timer. Without this the alarm rides for five days."""
    out = tmp_path / "signals.md"
    sig = _sig(ALARM, severity="nuclear", source="github-actions")
    sig.attrs["_parent_arc_created"] = NOW.strftime("%Y-%m-%d")
    sig.attrs["_resolved"] = "true"

    stats = brain_keeper.write_signals_feed(out, [sig], now=NOW)

    assert stats["tombstoned_resolved"] == 1
    assert stats["written"] == 0
    assert "6f8c941" not in out.read_text()


def test_an_unresolved_nuclear_signal_still_broadcasts(tmp_path: Path):
    out = tmp_path / "signals.md"
    sig = _sig(ALARM, severity="nuclear", source="github-actions")
    sig.attrs["_parent_arc_created"] = NOW.strftime("%Y-%m-%d")

    stats = brain_keeper.write_signals_feed(out, [sig], now=NOW)

    assert stats["written"] == 1
    assert stats["tombstoned_resolved"] == 0


# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------


def _signal_file(root: Path, name: str, created: datetime, severity: str, body: str) -> Path:
    d = root / "amygdala"
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_text(
        f"---\nid: {name[:-3]}\ntitle: t\nseverity: {severity}\nsource: ci\n"
        f"created: {created.isoformat(timespec='seconds')}\n---\n\n{body}\n"
    )
    return p


def test_an_ancient_signal_is_archived_not_deleted(tmp_path: Path):
    """A signal is content — it records what caught fire and often what was
    done about it. The tick queue can be purged; this has to be recoverable."""
    old = NOW - timedelta(days=brain_keeper.BRAIN_SIGNAL_RETAIN_DAYS + 10)
    p = _signal_file(tmp_path, "2026-04-09-ancient.md", old, "warning", "something burned")
    for i in range(brain_keeper.BRAIN_SIGNAL_KEEP_MIN):
        _signal_file(tmp_path, f"fresh-{i}.md", NOW, "warning", f"live {i}")

    stats = brain_keeper.sweep_signal_files(tmp_path, set())

    assert stats["archived_stale"] == 1
    assert not p.exists()
    archived = list((tmp_path / "_backups" / "amygdala").rglob("2026-04-09-ancient.md"))
    assert len(archived) == 1
    assert "something burned" in archived[0].read_text()


def test_a_closed_incident_ages_out_on_the_shorter_window(tmp_path: Path):
    age = (
        brain_keeper.BRAIN_SIGNAL_RESOLVED_RETAIN_DAYS + brain_keeper.BRAIN_SIGNAL_RETAIN_DAYS
    ) // 2
    when = NOW - timedelta(days=age)
    closed_file = _signal_file(tmp_path, "closed.md", when, "warning", ALARM)
    open_file = _signal_file(tmp_path, "open.md", when, "warning", "Run=77777777777 unrelated")
    for i in range(brain_keeper.BRAIN_SIGNAL_KEEP_MIN):
        _signal_file(tmp_path, f"fresh-{i}.md", NOW, "warning", f"live {i}")

    stats = brain_keeper.sweep_signal_files(tmp_path, {"3151857"})

    assert stats["archived_resolved"] == 1
    assert stats["archived_stale"] == 0
    assert not closed_file.exists()
    assert open_file.exists(), "an open incident keeps the long window"


def test_a_resolution_is_never_archived_before_the_alarm_it_closes(tmp_path: Path, monkeypatch):
    """Otherwise the sweep undoes its own work and the alarm flip-flops.

    Archive the closing document while the alarm it silenced is still inside
    its own broadcast window, and the next tick re-opens the alarm — a nuclear
    alert that comes back from the dead, which is the exact failure this whole
    change set out to kill. The two knobs move independently, so the floor has
    to be enforced rather than assumed.
    """
    monkeypatch.setattr(brain_keeper, "BRAIN_SIGNAL_RESOLVED_RETAIN_DAYS", 2)
    monkeypatch.setattr(brain_keeper, "BRAIN_STALE_CRITICAL_DAYS", 30)
    when = NOW - timedelta(days=10)
    closing = _signal_file(tmp_path, "closing.md", when, "resolved", RESOLUTION)
    for i in range(brain_keeper.BRAIN_SIGNAL_KEEP_MIN):
        _signal_file(tmp_path, f"fresh-{i}.md", NOW, "warning", f"live {i}")

    stats = brain_keeper.sweep_signal_files(tmp_path, {"3151857"})

    assert stats["archived_resolved"] == 0
    assert closing.exists(), "archived a resolution still holding an alarm closed"


def test_the_newest_signals_survive_any_age_rule(tmp_path: Path):
    """A quiet stretch must not leave an operator with no recent history."""
    old = NOW - timedelta(days=brain_keeper.BRAIN_SIGNAL_RETAIN_DAYS + 50)
    for i in range(brain_keeper.BRAIN_SIGNAL_KEEP_MIN):
        _signal_file(tmp_path, f"old-{i:03d}.md", old, "warning", f"ancient {i}")

    stats = brain_keeper.sweep_signal_files(tmp_path, set())

    assert stats["archived_stale"] == 0
    assert len(list((tmp_path / "amygdala").glob("*.md"))) == brain_keeper.BRAIN_SIGNAL_KEEP_MIN


def test_the_readme_is_furniture_not_a_signal(tmp_path: Path):
    (tmp_path / "amygdala").mkdir(parents=True)
    readme = tmp_path / "amygdala" / "README.md"
    readme.write_text("# amygdala\n\nwhat lives here\n")
    import os

    ancient = (NOW - timedelta(days=900)).timestamp()
    os.utime(readme, (ancient, ancient))

    brain_keeper.sweep_signal_files(tmp_path, set())

    assert readme.exists()


def test_a_dry_run_counts_without_moving_anything(tmp_path: Path):
    old = NOW - timedelta(days=brain_keeper.BRAIN_SIGNAL_RETAIN_DAYS + 10)
    p = _signal_file(tmp_path, "ancient.md", old, "warning", "x")
    for i in range(brain_keeper.BRAIN_SIGNAL_KEEP_MIN):
        _signal_file(tmp_path, f"fresh-{i}.md", NOW, "warning", f"live {i}")

    stats = brain_keeper.sweep_signal_files(tmp_path, set(), dry_run=True)

    assert stats["archived_stale"] == 1
    assert p.exists()


def test_a_missing_amygdala_directory_is_not_an_error(tmp_path: Path):
    assert brain_keeper.sweep_signal_files(tmp_path, set())["errors"] == 0


def test_retention_disabled_leaves_everything_alone(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(brain_keeper, "BRAIN_SIGNAL_RETAIN_DAYS", 0)
    monkeypatch.setattr(brain_keeper, "BRAIN_SIGNAL_RESOLVED_RETAIN_DAYS", 0)
    old = NOW - timedelta(days=900)
    p = _signal_file(tmp_path, "ancient.md", old, "warning", "x")

    assert brain_keeper.sweep_signal_files(tmp_path, set()) == {
        "archived_resolved": 0,
        "archived_stale": 0,
        "errors": 0,
    }
    assert p.exists()


# ---------------------------------------------------------------------------
# End to end through the tick
# ---------------------------------------------------------------------------


def test_a_tick_closes_an_alarm_whose_resolution_is_in_the_vault(tmp_path: Path):
    """The whole point, exercised through the real tick.

    An arc carrying a nuclear @signal, and a second arc carrying the closing
    @signal — the alarm must not reach the feed, and the resolution must, or
    the operator has no record that it was answered.
    """
    vault = tmp_path / "vault"
    (vault / "clusters" / "2026-08-11").mkdir(parents=True)
    (vault / "left").mkdir(parents=True)
    feed = vault / "brain-feed"
    feed.mkdir()
    stamp = NOW.strftime("%Y-%m-%d")

    (vault / "clusters" / "2026-08-11" / f"{stamp}-deploy-failed.md").write_text(
        f"---\ncluster_id: {stamp}-deploy-failed\ncreated: {stamp}\nstatus: active\n---\n\n"
        f"<!-- @signal severity=nuclear source=github-actions -->\n{ALARM}\n<!-- @/signal -->\n"
    )
    (vault / "clusters" / "2026-08-11" / f"{stamp}-deploy-fixed.md").write_text(
        f"---\ncluster_id: {stamp}-deploy-fixed\ncreated: {stamp}\nstatus: active\n---\n\n"
        f"<!-- @signal severity=resolved source=github-actions -->\n{RESOLUTION}\n"
        "<!-- @/signal -->\n"
    )

    stats = brain_keeper.tick(vault, feed, dry_run=False, quick_refresh=False)
    body = (feed / "signals.md").read_text()

    assert stats["signals_tombstoned_resolved"] == 1
    assert "Branch=dev SHA=6f8c941ebcb" not in body, "the alarm is closed"
    assert "is resolved" in body, "the closing signal itself must still be reported"


def test_the_closing_signal_does_not_tombstone_itself(tmp_path: Path):
    """A resolved marker names its own incident. Without the guard it matches
    its own keys, tombstones itself, and the operator sees nothing at all."""
    vault = tmp_path / "vault"
    (vault / "clusters" / "2026-08-11").mkdir(parents=True)
    (vault / "left").mkdir(parents=True)
    feed = vault / "brain-feed"
    feed.mkdir()
    stamp = NOW.strftime("%Y-%m-%d")
    (vault / "clusters" / "2026-08-11" / f"{stamp}-fixed.md").write_text(
        f"---\ncluster_id: {stamp}-fixed\ncreated: {stamp}\nstatus: active\n---\n\n"
        f"<!-- @signal severity=resolved source=ci -->\n{RESOLUTION}\n<!-- @/signal -->\n"
    )

    stats = brain_keeper.tick(vault, feed, dry_run=False, quick_refresh=False)

    assert stats["signals_written"] == 1
    assert stats["signals_tombstoned_resolved"] == 0
