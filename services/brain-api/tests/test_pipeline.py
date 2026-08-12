"""Tests for the pipeline health report.

The property under test throughout is that a verdict tracks the *evidence*,
not the shape of the vault: an unused brain is healthy, a recovered brain is
healthy, and a stage fails only when its input exists and its output does not.
Getting that wrong in either direction is what makes a health command
ignorable — false alarms train the operator to skip it, and a green report over
a dead loop is worse than no report.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app import pipeline

NOW = datetime.now(tz=timezone.utc)


def _vault(tmp_path: Path) -> Path:
    for rel in (
        "brain-feed/ticks/requested",
        "brain-feed/ticks/completed",
        "brain-feed/ticks/failed",
        "left/reference",
        "clusters",
        "amygdala",
    ):
        (tmp_path / rel).mkdir(parents=True, exist_ok=True)
    return tmp_path


def _feed_file(root: Path, name: str, fid: str, body: str, ttl: int = 3600) -> Path:
    p = root / "brain-feed" / name
    p.write_text(
        f"---\nid: {fid}\ntitle: {fid}\npriority: 5\nttl: {ttl}\nseverity: info\n---\n\n{body}\n"
    )
    return p


def _request(root: Path, state: str, job: str, requested_at: datetime, **extra) -> Path:
    p = root / "brain-feed" / "ticks" / state / f"{job}.json"
    payload = {
        "job_id": job,
        "requested_at": requested_at.isoformat(timespec="seconds"),
        "status": "pending",
        **extra,
    }
    p.write_text(json.dumps(payload))
    return p


# ---------------------------------------------------------------------------
# The empty-vault property
# ---------------------------------------------------------------------------


def test_fresh_vault_is_healthy_not_broken(tmp_path: Path):
    """A brain nobody has used yet must not report as broken.

    Every stage has zero input here. Reporting that as failure is the fastest
    way to make `agentibrain check` something the operator stops running.
    """
    report = pipeline.pipeline_report(vault_root=_vault(tmp_path), now=NOW, index_stats=None)
    assert report["status"] in {"ok", "degraded"}
    for name in ("ingest", "drain", "arcs", "lessons", "signals", "feed"):
        assert report["stages"][name]["status"] == "ok", (
            f"{name} reported {report['stages'][name]} on an unused vault"
        )


# ---------------------------------------------------------------------------
# Stage 2 — drain
# ---------------------------------------------------------------------------


def test_wedged_queue_fails_with_the_age_that_proves_it(tmp_path: Path):
    root = _vault(tmp_path)
    _request(root, "requested", "stuck01", NOW - timedelta(minutes=42))

    stage = pipeline.check_drain(root, NOW)

    assert stage["status"] == "fail"
    assert stage["pending"] == 1
    assert stage["oldest_pending_age_minutes"] >= 41
    assert "tick-drain" in stage["hint"]


def test_recent_request_is_not_yet_a_failure(tmp_path: Path):
    """The drain gets a grace window — a request enqueued seconds ago is normal."""
    root = _vault(tmp_path)
    _request(root, "requested", "fresh01", NOW - timedelta(seconds=5))
    assert pipeline.check_drain(root, NOW)["status"] == "ok"


def test_last_tick_failed_surfaces_its_error_tail(tmp_path: Path):
    """The operator's real case: drain works, tick dies on the LLM call.

    Reporting only "the queue is moving" here would be true and useless — the
    reason lives in error_tail and must reach the report.
    """
    root = _vault(tmp_path)
    _request(
        root,
        "failed",
        "boom01",
        NOW - timedelta(minutes=3),
        error_tail="urllib.request ...\nTimeoutError: timed out\n",
    )

    stage = pipeline.check_drain(root, NOW)

    assert stage["status"] == "fail"
    assert stage["last_resolution"] == "failed"
    assert "TimeoutError" in stage["error_tail"]


def test_failure_followed_by_success_reads_as_recovered(tmp_path: Path):
    """An old pile of failures under a fresh success is a working stack.

    Written in ctime order — the failed record first — because that is what
    `_resolution_time` reads, and it is the only ordering a test can create
    without backdating ctime.
    """
    root = _vault(tmp_path)
    _request(root, "failed", "old01", NOW - timedelta(hours=5), error_tail="boom")
    _request(root, "completed", "new01", NOW - timedelta(minutes=2))

    stage = pipeline.check_drain(root, NOW)

    assert stage["status"] == "ok"
    assert stage["last_resolution"] == "completed"
    assert stage["failed"] == 1


def test_long_silence_since_last_success_warns(tmp_path: Path, monkeypatch):
    """A tick cron that stopped firing. ctime cannot be backdated, so the
    resolution clock is stubbed — the verdict logic is what is under test."""
    root = _vault(tmp_path)
    old = _request(root, "completed", "stale1", NOW - timedelta(days=2))
    monkeypatch.setattr(
        pipeline,
        "_resolution_time",
        lambda p: (NOW - timedelta(days=2)).timestamp() if p == old else p.stat().st_mtime,
    )

    stage = pipeline.check_drain(root, NOW)

    assert stage["status"] == "warn"
    assert stage["last_tick_activity_hours"] > pipeline.TICK_STALE_HOURS


def test_a_cron_tick_counts_even_though_it_leaves_no_queue_record(tmp_path: Path, monkeypatch):
    """The 2h cron calls brain_tick.py directly — nothing lands in completed/.

    Measuring tick freshness from the queue alone reported a busy production
    stack as stale, because the queue only records ticks someone asked for over
    HTTP. A regenerated feed file is the evidence a tick actually ran.
    """
    import os

    root = _vault(tmp_path)
    old = _request(root, "completed", "ancient", NOW - timedelta(days=4))
    monkeypatch.setattr(
        pipeline,
        "_resolution_time",
        lambda p: (NOW - timedelta(days=4)).timestamp() if p == old else p.stat().st_mtime,
    )
    fresh = _feed_file(root, "hot-arcs.md", "hot-arcs", "| 2026-08-12-x | 7 |")
    os.utime(fresh, None)

    stage = pipeline.check_drain(root, NOW)

    assert stage["status"] == "ok", "a stack ticking on cron must not read as stale"
    assert stage["last_tick_activity_hours"] < 1


def test_resolution_time_prefers_whichever_stat_is_later(tmp_path: Path):
    """`mv` keeps mtime and moves ctime — the later of the two is the arrival."""
    import os

    p = tmp_path / "rec.json"
    p.write_text("{}")
    old = (NOW - timedelta(days=3)).timestamp()
    os.utime(p, (old, old))
    st = p.stat()
    assert pipeline._resolution_time(p) == max(st.st_mtime, st.st_ctime)
    assert pipeline._resolution_time(p) > old, "a moved record must not read as old"


# ---------------------------------------------------------------------------
# Stage 4 — lessons
# ---------------------------------------------------------------------------


def _lesson_log(root: Path, rel: str, when: datetime) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    stamp = when.strftime("%Y-%m-%d")
    p.write_text(
        f"---\nid: lessons-{stamp}\ntitle: Lessons — {stamp}\n"
        f"type: lesson-log\ncreated: {stamp}\n---\n\n"
        f"## {when.isoformat(timespec='seconds')} — agent\n\n"
        "A lesson with enough substance to be worth keeping.\n"
    )
    return p


def test_a_lesson_log_outside_left_reference_fails(tmp_path: Path):
    """Scatter is the defect that made months of lessons unreachable.

    A log the graduation pass relocated is invisible to both the feed writer
    and the lesson embedder, so it is silently gone while the file still exists.
    """
    root = _vault(tmp_path)
    _lesson_log(root, "left/reference/lessons-2026-08-11.md", NOW)
    _lesson_log(root, "frontal-lobe/unconscious/lessons-2026-08-10.md", NOW - timedelta(days=1))
    _feed_file(root, "lessons.md", "lessons", "- a lesson")

    stage = pipeline.check_lessons(root, NOW)

    assert stage["status"] == "fail"
    assert stage["scattered_logs"] == 1
    assert "frontal-lobe/unconscious/lessons-2026-08-10.md" in stage["scattered_paths"]


def test_a_file_merely_named_like_a_lesson_log_is_not_scatter(tmp_path: Path):
    """`lessons-learned-notes.md` is somebody's idea doc, not a lesson log.

    The reconcile matches an anchored, date-shaped name and correctly refuses
    to touch anything else — so flagging such a file produced a failure no tick
    could ever clear, with a hint blaming a phase that was working fine.
    """
    root = _vault(tmp_path)
    _lesson_log(root, "left/reference/lessons-2026-08-11.md", NOW)
    (root / "right" / "ideas").mkdir(parents=True)
    (root / "right" / "ideas" / "lessons-learned-notes.md").write_text("# an idea, named oddly\n")
    _feed_file(root, "lessons.md", "lessons", "- a lesson")

    stage = pipeline.check_lessons(root, NOW)

    assert stage["scattered_logs"] == 0
    assert stage["status"] == "ok"


def test_backups_do_not_count_as_scatter(tmp_path: Path):
    """The reconcile's own safety copies must not read as the fault they guard."""
    root = _vault(tmp_path)
    _lesson_log(root, "left/reference/lessons-2026-08-11.md", NOW)
    _lesson_log(root, "left/_backups/lesson-reconcile/x/lessons-2026-08-10.md", NOW)
    _feed_file(root, "lessons.md", "lessons", "- a lesson")

    assert pipeline.check_lessons(root, NOW)["scattered_logs"] == 0


def test_lessons_written_but_never_fed_back_fails(tmp_path: Path):
    root = _vault(tmp_path)
    _lesson_log(root, "left/reference/lessons-2026-08-11.md", NOW)

    stage = pipeline.check_lessons(root, NOW)

    assert stage["status"] == "fail"
    assert "lessons.md" in stage["hint"]


def test_recent_lessons_with_an_empty_feed_fails(tmp_path: Path):
    """The exact end state of the original bug: 364 lessons, feed says nothing."""
    root = _vault(tmp_path)
    _lesson_log(root, "left/reference/lessons-2026-08-11.md", NOW)
    _feed_file(root, "lessons.md", "lessons", "No recent lessons.")

    stage = pipeline.check_lessons(root, NOW)

    assert stage["status"] == "fail"
    assert stage["recent_in_window"] == 1


def test_healthy_lesson_pipeline_passes(tmp_path: Path):
    root = _vault(tmp_path)
    _lesson_log(root, "left/reference/lessons-2026-08-11.md", NOW)
    _feed_file(root, "lessons.md", "lessons", "- **2026-08-11** something learned")

    stage = pipeline.check_lessons(root, NOW)

    assert stage["status"] == "ok"
    assert stage["logs"] == 1
    assert stage["entries"] == 1
    assert stage["feed_entries"] == 1


def test_lessons_older_than_the_window_do_not_force_a_failure(tmp_path: Path):
    """Nothing recent to say is not the same as a broken writer."""
    root = _vault(tmp_path)
    _lesson_log(
        root,
        "left/reference/lessons-old.md",
        NOW - timedelta(days=pipeline.LESSON_WINDOW_DAYS + 10),
    )
    _feed_file(root, "lessons.md", "lessons", "No recent lessons.")

    assert pipeline.check_lessons(root, NOW)["status"] == "ok"


# ---------------------------------------------------------------------------
# Stage 5 — signals
# ---------------------------------------------------------------------------


def test_the_amygdala_readme_is_not_counted_as_a_signal(tmp_path: Path):
    """The scaffold seeds amygdala/README.md, and counting it pinned
    `oldest_signal_age_days` to the day the vault was created — 122 days on a
    live vault whose oldest actual alarm was 71."""
    import os

    root = _vault(tmp_path)
    readme = root / "amygdala" / "README.md"
    readme.write_text("# amygdala\n\nwhat lives here\n")
    os.utime(readme, ((NOW - timedelta(days=400)).timestamp(),) * 2)
    (root / "amygdala" / "2026-08-12-real.md").write_text("---\nseverity: warning\n---\nx")
    _feed_file(root, "signals.md", "signals", "- **[warning]** (ci) something")

    stage = pipeline.check_signals(root, NOW)

    assert stage["signal_files"] == 1
    assert stage["oldest_signal_age_days"] < 1


def test_signals_raised_but_never_broadcast_fails(tmp_path: Path):
    root = _vault(tmp_path)
    (root / "amygdala" / "2026-08-12-nuclear-ci-red.md").write_text(
        "---\nseverity: nuclear\n---\nx"
    )

    stage = pipeline.check_signals(root, NOW)

    assert stage["status"] == "fail"
    assert stage["signal_files"] == 1


def test_a_stale_broadcast_file_warns_because_its_ttl_sweep_never_ran(tmp_path: Path):
    """The operator's stale-nuclear-alert complaint, stated as a precondition.

    Whether one entry has outlived its TTL is the tick's call; what this
    establishes is that the file carrying it has not been revisited, which
    makes every entry in it untrustworthy regardless.
    """
    import os

    root = _vault(tmp_path)
    (root / "amygdala" / "2026-08-05-nuclear-old.md").write_text("---\nseverity: nuclear\n---\nx")
    feed = _feed_file(root, "signals.md", "signals", "- **[nuclear]** (github-actions) CI red")
    old = (NOW - timedelta(hours=pipeline.FEED_STALE_HOURS + 4)).timestamp()
    os.utime(feed, (old, old))

    stage = pipeline.check_signals(root, NOW)

    assert stage["status"] == "warn"
    assert stage["broadcast_by_severity"] == {"nuclear": 1}
    assert "TTL sweep" in stage["hint"]


# ---------------------------------------------------------------------------
# Stage 6 — feed
# ---------------------------------------------------------------------------


def test_feed_files_without_frontmatter_reach_nobody(tmp_path: Path):
    root = _vault(tmp_path)
    (root / "brain-feed" / "hot-arcs.md").write_text("## Hot Arcs\n\nno frontmatter here\n")

    stage = pipeline.check_feed(root, NOW)

    assert stage["status"] == "fail"
    assert stage["served_entries"] == 0


def test_a_feed_directory_gone_completely_cold_warns(tmp_path: Path):
    import os

    root = _vault(tmp_path)
    f = _feed_file(root, "hot-arcs.md", "hot-arcs", "| 2026-08-11-x | 7 |")
    old = (NOW - timedelta(hours=pipeline.FEED_STALE_HOURS + 3)).timestamp()
    os.utime(f, (old, old))

    stage = pipeline.check_feed(root, NOW)

    assert stage["status"] == "warn"
    assert stage["freshest_age_hours"] > pipeline.FEED_STALE_HOURS


def test_one_old_feed_file_beside_a_fresh_one_is_not_a_fault(tmp_path: Path):
    """`ttl` is a consumer cache hint, not a rewrite contract.

    intent.md and last-tick-diff.md are written only by the AI phase, and only
    when it has something to say. Comparing their age to their own short ttl
    flagged a perfectly healthy production stack, which is exactly the kind of
    false alarm that gets a health command ignored.
    """
    import os

    root = _vault(tmp_path)
    stale = _feed_file(root, "intent.md", "operator-intent", "the long game", ttl=1800)
    os.utime(stale, ((NOW - timedelta(hours=6)).timestamp(),) * 2)
    _feed_file(root, "hot-arcs.md", "hot-arcs", "| 2026-08-12-x | 7 |")

    stage = pipeline.check_feed(root, NOW)

    assert stage["status"] == "ok"
    assert stage["age_hours"]["operator-intent"] >= 6


def test_readme_alongside_feed_files_is_not_counted_as_breakage(tmp_path: Path):
    """The scaffold ships brain-feed/README.md; it has no frontmatter by design."""
    root = _vault(tmp_path)
    (root / "brain-feed" / "README.md").write_text("# brain-feed\n\ndocs\n")
    _feed_file(root, "hot-arcs.md", "hot-arcs", "| 2026-08-11-x | 7 |")

    assert pipeline.check_feed(root, NOW)["status"] == "ok"


# ---------------------------------------------------------------------------
# Stage 7 — index
# ---------------------------------------------------------------------------


def test_a_producer_with_source_files_and_no_rows_fails(tmp_path: Path):
    """Silent by construction: search returns fewer hits, never an error."""
    root = _vault(tmp_path)
    _lesson_log(root, "left/reference/lessons-2026-08-11.md", NOW)

    stage = pipeline.check_index(
        root,
        {"producers": [{"producer": "brain-arc", "keys": 12, "rows": 40}], "total_rows": 40},
    )

    assert stage["status"] == "fail"
    assert stage["missing_producers"] == ["brain-lesson"]


def test_index_counts_keys_not_chunk_rows(tmp_path: Path):
    """One long document produces many rows; coverage is a question about keys.

    The fixture has to make the two disagree — `keys=0, rows=5` — or the test
    passes identically whether the code branches on keys or on rows, and proves
    neither.
    """
    root = _vault(tmp_path)
    _lesson_log(root, "left/reference/lessons-2026-08-11.md", NOW)
    (root / "clusters" / "2026-08-01").mkdir(parents=True, exist_ok=True)
    (root / "clusters" / "2026-08-01" / "arc.md").write_text("---\ncluster_id: a\n---\nx")

    stage = pipeline.check_index(
        root,
        {
            "producers": [
                {"producer": "brain-arc", "keys": 4, "rows": 4},
                {"producer": "brain-lesson", "keys": 0, "rows": 5},
            ],
            "total_rows": 9,
        },
    )

    assert stage["status"] == "fail", "rows>0 with keys=0 means nothing is indexed"
    assert stage["missing_producers"] == ["brain-lesson"]


def test_a_graduated_arc_outside_left_still_counts_as_an_index_source(tmp_path: Path):
    """Graduation files arcs into any of the six regions, and nested.

    Counting only `left/*.md` reported a completely dead embedder as healthy:
    five real arcs in right/, zero rows in the index, verdict ok. That is the
    single worst outcome this report can produce.
    """
    root = _vault(tmp_path)
    (root / "right" / "ideas").mkdir(parents=True)
    for i in range(5):
        (root / "right" / "ideas" / f"arc-{i}.md").write_text(
            f"---\ncluster_id: arc-{i}\nheat: 7\n---\n\nreal content\n"
        )
    _feed_file(root, "hot-arcs.md", "hot-arcs", "| 2026-08-12-x | 7 |")

    stage = pipeline.check_index(root, {"producers": [], "total_rows": 0})

    assert stage["status"] == "fail"
    assert "brain-arc" in stage["missing_producers"]


def test_a_scaffolded_vault_that_has_never_ticked_is_not_a_failure(tmp_path: Path):
    """The shipped scaffold seeds standing region docs before anything runs.

    Those are real embedder input, so they must count as a source — but on a
    fresh install nothing has ticked yet, and calling a cold index broken is a
    false alarm on the very first command a new user types.
    """
    root = _vault(tmp_path)
    (root / "bridge").mkdir(exist_ok=True)
    (root / "bridge" / "vision.md").write_text("---\ntitle: Vision\n---\n\nthe long game\n")

    stage = pipeline.check_index(root, {"producers": [], "total_rows": 0})

    assert stage["status"] == "ok"
    assert "no tick has run" in stage["note"]


def test_unreachable_index_degrades_rather_than_failing(tmp_path: Path):
    """An older embeddings image has no /stats — unverified is not broken."""
    assert pipeline.check_index(_vault(tmp_path), None)["status"] == "warn"
    assert pipeline.check_index(_vault(tmp_path), {"error": "404"})["status"] == "warn"


def test_a_null_key_count_is_read_as_zero_not_as_healthy(tmp_path: Path):
    """A producer row missing its count must not pass by omission.

    `.get("keys", 0)` returns None — not 0 — when the key is present and null,
    and `None == 0` is false, which would quietly clear the check.
    """
    root = _vault(tmp_path)
    _lesson_log(root, "left/reference/lessons-2026-08-11.md", NOW)
    (root / "clusters" / "2026-08-01").mkdir(parents=True, exist_ok=True)
    (root / "clusters" / "2026-08-01" / "arc.md").write_text("---\ncluster_id: a\n---\nx")

    # brain-arc indexed, so this exercises the per-producer branch rather than
    # the whole-index-cold one.
    stage = pipeline.check_index(
        root,
        {
            "producers": [
                {"producer": "brain-arc", "keys": 3, "rows": 3},
                {"producer": "brain-lesson", "keys": None, "rows": 0},
            ]
        },
    )

    assert stage["status"] == "fail"
    assert stage["missing_producers"] == ["brain-lesson"]


def test_an_unexpected_stats_shape_degrades_rather_than_raising(tmp_path: Path):
    """A service answering 200 with the wrong body is a config fault, not a crash."""
    for body in ({"producers": "everything"}, {"producers": None}, {}):
        assert pipeline.check_index(_vault(tmp_path), body)["status"] == "warn"


def test_scan_is_bounded_so_vault_size_cannot_stall_the_endpoint(tmp_path: Path, monkeypatch):
    """The vault is an NFS mount in production and this endpoint is pollable.

    Truncation is safe because every count here feeds a threshold comparison,
    never an inventory — but it has to actually happen.
    """
    root = _vault(tmp_path)
    for i in range(40):
        (root / "clusters" / f"2026-08-{i:02d}").mkdir(parents=True, exist_ok=True)
        (root / "clusters" / f"2026-08-{i:02d}" / f"arc-{i}.md").write_text("x")
    monkeypatch.setattr(pipeline, "MAX_SCAN", 5)

    assert len(pipeline._glob(root, "clusters/*/*.md")) == 5
    assert len(pipeline._glob(root, "clusters/*/*.md", limit=1)) == 1


def test_scatter_walk_finds_a_deeply_nested_log(tmp_path: Path):
    """Graduation can nest a log below a region root; the walk must reach it."""
    root = _vault(tmp_path)
    _lesson_log(root, "left/reference/lessons-2026-08-11.md", NOW)
    _lesson_log(root, "right/a/b/c/lessons-2026-08-09.md", NOW - timedelta(days=2))

    assert pipeline._scattered_lessons(root) == ["right/a/b/c/lessons-2026-08-09.md"]


def test_empty_vault_expects_no_producers(tmp_path: Path):
    assert (
        pipeline.check_index(_vault(tmp_path), {"producers": [], "total_rows": 0})["status"] == "ok"
    )


# ---------------------------------------------------------------------------
# Report assembly + endpoint
# ---------------------------------------------------------------------------


def test_one_exploding_stage_does_not_sink_the_report(tmp_path: Path, monkeypatch):
    """A partial report still says where to look; a 500 says nothing."""

    def _boom(root, now):
        raise RuntimeError("stat storm")

    monkeypatch.setattr(pipeline, "_STAGES", (("arcs", _boom), ("feed", pipeline.check_feed)))

    report = pipeline.pipeline_report(vault_root=_vault(tmp_path), now=NOW)

    assert report["status"] == "broken"
    assert "stat storm" in report["stages"]["arcs"]["error"]
    assert report["stages"]["feed"]["status"] == "ok"


def test_overall_status_takes_the_worst_stage(tmp_path: Path):
    root = _vault(tmp_path)
    _lesson_log(root, "left/reference/lessons-2026-08-11.md", NOW)  # no feed → fail
    report = pipeline.pipeline_report(vault_root=root, now=NOW)
    assert report["status"] == "broken"
    assert report["stages"]["lessons"]["status"] == "fail"


@pytest.mark.parametrize("path", ["/health/pipeline"])
def test_endpoint_returns_a_full_report(vault: Path, client, path: str):
    resp = client.get(path)
    assert resp.status_code == 200
    body = resp.json()
    assert body["service"] == "brain-api"
    assert set(body["stages"]) == {
        "ingest",
        "drain",
        "arcs",
        "lessons",
        "signals",
        "feed",
        "index",
    }
    assert body["status"] in {"ok", "degraded", "broken"}
