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
    assert sr.incident_keys(ALARM) & sr.incident_keys(RESOLUTION) == {"31518571119", "6f8c941"}


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


def test_a_resolved_arc_closes_by_its_summary():
    arc = _doc("incident.md", {"status": "resolved", "summary": RESOLUTION})
    assert sr.is_resolved(ALARM, sr.resolved_keys([arc], []))


def test_a_resolved_arc_does_not_close_what_its_body_merely_mentions():
    """A good postmortem names every run it investigated, including the ones it
    ruled out. Scraping the narrative closed those too — so the act of writing a
    thorough postmortem silenced unrelated live alarms."""
    arc = _doc(
        "postmortem.md",
        {"status": "resolved", "summary": "root cause for run 31518534521"},
        body=(
            "We also looked at runs 31518512340 and 31518598877 but ruled them out "
            "as unrelated flakes. Root cause for THIS incident was a stale cache key."
        ),
    )
    closed = sr.resolved_keys([arc], [])

    assert "31518534521" in closed, "the incident it actually resolved"
    assert "31518512340" not in closed, "explicitly ruled out"
    assert "31518598877" not in closed, "explicitly ruled out"


def test_a_resolved_severity_file_asserts_with_its_whole_body():
    """Unlike an arc, one of these is single-purpose — the document IS the
    claim, and the identifier normally sits in the prose, not the frontmatter."""
    doc = _doc("amygdala/x.md", {"severity": "resolved"}, body=RESOLUTION)
    assert sr.is_resolved(ALARM, sr.resolved_keys([doc], []))


def test_an_explicit_resolves_field_is_honoured():
    """The precise instrument, for an author who wants no inference at all."""
    arc = _doc("fix.md", {"status": "resolved", "resolves": "31518571119"})
    assert sr.is_resolved(ALARM, sr.resolved_keys([arc], []))
    arc_list = _doc("fix.md", {"status": "resolved", "resolves": ["31518571119", "6f8c941e"]})
    assert sr.is_resolved(ALARM, sr.resolved_keys([arc_list], []))


def test_two_github_runs_from_the_same_day_are_different_incidents():
    """Run ids are a near-sequential global counter, so two runs minutes apart
    share their leading digits. Keying on a 7-char prefix did not identify an
    incident — it identified a window of ~10,000 runs, and a resolved cache bug
    silently closed a live checkout outage."""
    live = "Run=31518579981 checkout is down, orders failing"
    other = "Run=31518571119 stale cache key. Fixed in 9f1c2ab."
    assert not (sr.incident_keys(live) & sr.incident_keys(other))


def test_a_short_sha_still_matches_its_full_form():
    """Truncation IS right for hex: git abbreviates, slugs truncate, humans
    quote the short one. Seven hex characters is 268M values, not a counter."""
    assert sr.incident_keys("SHA=6f8c941ebcb1582d055c308e67d040fbfd8410c2") & sr.incident_keys(
        "(SHA 6f8c941e)"
    )


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

    stats = brain_keeper.sweep_signal_files(tmp_path, {"31518571119"})

    assert stats["archived_resolved"] == 1
    assert stats["archived_stale"] == 0
    assert not closed_file.exists()
    assert open_file.exists(), "an open incident keeps the long window"


def test_a_closing_document_keeps_the_long_window(tmp_path: Path):
    """The shorter window belongs to the answered alarm, never to the answer.

    `closed_incidents` is rebuilt from scratch every tick from whatever is still
    scanned, so archiving the closer deletes the only key that closes its
    incident. Closers are cheap — one per incident, against one per re-emission.
    """
    when = NOW - timedelta(days=brain_keeper.BRAIN_SIGNAL_RESOLVED_RETAIN_DAYS + 20)
    closer = _signal_file(tmp_path, "closer.md", when, "resolved", RESOLUTION)
    answered = _signal_file(tmp_path, "answered.md", when, "warning", ALARM)
    for i in range(brain_keeper.BRAIN_SIGNAL_KEEP_MIN):
        _signal_file(tmp_path, f"fresh-{i}.md", NOW, "warning", f"live {i}")

    stats = brain_keeper.sweep_signal_files(tmp_path, {"31518571119"})

    assert closer.exists(), "archiving the answer destroys the only record of it"
    assert not answered.exists(), "the answered alarm still ages out fast"
    assert stats["archived_resolved"] == 1


def test_a_late_duplicate_alarm_is_still_closed_after_a_sweep(tmp_path: Path):
    """The resurrection case, across two ticks with a real archive between them.

    One CI failure left seven files in this directory, so a duplicate emission
    of the same alarm arriving later is routine, not exotic. If the sweep has
    archived the answer by then, the late copy finds nothing that closes it and
    broadcasts nuclear for a condition the vault already answered. The age sweep
    cannot save it either — that measures the ALARM's age, and this copy is new.

    Every previous tick-level test here ran a single tick with fewer files than
    KEEP_MIN, so the sweep never moved anything and this was invisible.
    """
    vault = tmp_path / "vault"
    (vault / "clusters").mkdir(parents=True)
    (vault / "left").mkdir(parents=True)
    feed = vault / "brain-feed"
    feed.mkdir()

    old = NOW - timedelta(days=brain_keeper.BRAIN_SIGNAL_RESOLVED_RETAIN_DAYS + 20)
    _signal_file(vault, "closer.md", old, "resolved", RESOLUTION)
    for i in range(brain_keeper.BRAIN_SIGNAL_KEEP_MIN):
        _signal_file(vault, f"filler-{i}.md", NOW, "info", f"routine {i}")

    brain_keeper.tick(vault, feed, dry_run=False, quick_refresh=False)

    # Tick 2: the same alarm fires again, brand new, carrying the same run id.
    stamp = NOW.strftime("%Y-%m-%d")
    (vault / "clusters" / stamp).mkdir(parents=True, exist_ok=True)
    (vault / "clusters" / stamp / f"{stamp}-deploy-failed.md").write_text(
        f"---\ncluster_id: {stamp}-deploy-failed\ncreated: {stamp}\nstatus: active\n---\n\n"
        f"<!-- @signal severity=nuclear source=github-actions -->\n{ALARM}\n<!-- @/signal -->\n"
    )

    stats = brain_keeper.tick(vault, feed, dry_run=False, quick_refresh=False)

    assert stats["signals_tombstoned_resolved"] == 1, "the answer must survive to close it"
    assert "SHA=6f8c941ebcb" not in (feed / "signals.md").read_text()


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


def test_graduation_does_not_erase_a_resolution_before_it_is_applied(tmp_path: Path):
    """The ordering bug, end to end.

    A postmortem marked `status: resolved`, still in clusters/ and old enough to
    graduate, had its status rewritten to `graduated` in memory before the
    resolution index was built. The closure was discarded without ever being
    applied once, and the alarm kept broadcasting. Reproduced with the
    resolution backdated past the graduation age.
    """
    vault = tmp_path / "vault"
    old_day = (NOW - timedelta(days=20)).strftime("%Y-%m-%d")
    (vault / "clusters" / old_day).mkdir(parents=True)
    (vault / "clusters" / NOW.strftime("%Y-%m-%d")).mkdir(parents=True)
    (vault / "left").mkdir(parents=True)
    feed = vault / "brain-feed"
    feed.mkdir()
    stamp = NOW.strftime("%Y-%m-%d")

    (vault / "clusters" / stamp / f"{stamp}-deploy-failed.md").write_text(
        f"---\ncluster_id: {stamp}-deploy-failed\ncreated: {stamp}\nstatus: active\n---\n\n"
        f"<!-- @signal severity=nuclear source=github-actions -->\n{ALARM}\n<!-- @/signal -->\n"
    )
    postmortem = vault / "clusters" / old_day / f"{old_day}-deploy-postmortem.md"
    postmortem.write_text(
        f"---\ncluster_id: {old_day}-deploy-postmortem\ncreated: {old_day}\n"
        f"status: resolved\nheat: 0\nsummary: {RESOLUTION}\n---\n\nthe write-up\n"
    )

    stats = brain_keeper.tick(vault, feed, dry_run=False, quick_refresh=False)

    assert stats["signals_tombstoned_resolved"] == 1, "the resolution must be applied"
    assert "Branch=dev SHA=6f8c941ebcb" not in (feed / "signals.md").read_text()


def test_graduation_files_a_resolved_arc_without_relabelling_it(tmp_path: Path):
    """Graduation moves an arc; it must not erase what the arc WAS."""
    vault = tmp_path / "vault"
    old_day = (NOW - timedelta(days=30)).strftime("%Y-%m-%d")
    (vault / "clusters" / old_day).mkdir(parents=True)
    (vault / "left").mkdir(parents=True)
    feed = vault / "brain-feed"
    feed.mkdir()
    (vault / "clusters" / old_day / f"{old_day}-fixed.md").write_text(
        f"---\ncluster_id: {old_day}-fixed\ncreated: {old_day}\nstatus: resolved\nheat: 0\n"
        "---\n\nclosed long ago\n"
    )

    brain_keeper.tick(vault, feed, dry_run=False, quick_refresh=False)

    moved = vault / "left" / f"{old_day}-fixed.md"
    assert moved.exists(), "it should still graduate out of clusters/"
    assert "status: resolved" in moved.read_text()
    assert "status: graduated" not in moved.read_text()


def test_an_ordinary_active_arc_still_graduates_to_graduated(tmp_path: Path):
    vault = tmp_path / "vault"
    old_day = (NOW - timedelta(days=30)).strftime("%Y-%m-%d")
    (vault / "clusters" / old_day).mkdir(parents=True)
    (vault / "left").mkdir(parents=True)
    feed = vault / "brain-feed"
    feed.mkdir()
    (vault / "clusters" / old_day / f"{old_day}-cold.md").write_text(
        f"---\ncluster_id: {old_day}-cold\ncreated: {old_day}\nstatus: active\nheat: 0\n"
        "---\n\nold work\n"
    )

    brain_keeper.tick(vault, feed, dry_run=False, quick_refresh=False)

    assert "status: graduated" in (vault / "left" / f"{old_day}-cold.md").read_text()
