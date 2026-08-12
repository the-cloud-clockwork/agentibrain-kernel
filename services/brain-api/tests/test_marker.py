"""Tests for POST /marker — four marker types + idempotency."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path


def test_marker_rejects_invalid_type(client):
    resp = client.post("/marker", json={"type": "gossip", "content": "..."})
    assert resp.status_code == 400


def test_marker_rejects_empty_content(client):
    resp = client.post("/marker", json={"type": "lesson", "content": ""})
    assert resp.status_code == 400


def test_marker_lesson_appends(vault: Path, client):
    body = {
        "type": "lesson",
        "content": "NFS dirs created by root need chmod 777 for UID 1000 writers.",
        "attrs": {"source": "deploy", "session_id": "abc123"},
    }
    resp = client.post("/marker", json=body)
    assert resp.status_code == 201
    data = resp.json()
    assert data["ok"] is True
    assert data["marker_type"] == "lesson"
    assert data["action"] == "appended"
    today = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
    assert data["vault_path"] == f"left/reference/lessons-{today}.md"

    written = (vault / data["vault_path"]).read_text()
    assert "NFS dirs" in written
    assert "abc123" in written

    # Second lesson appends (not overwrites).
    body2 = {"type": "lesson", "content": "Second lesson.", "attrs": {"source": "repro"}}
    resp2 = client.post("/marker", json=body2)
    assert resp2.status_code == 201
    assert (vault / data["vault_path"]).read_text().count("##") >= 2


def test_marker_milestone_daily_fallback(vault: Path, client):
    body = {
        "type": "milestone",
        "content": "Stream 1A shipped.",
        "attrs": {"source": "phase-7", "status": "done", "scope": "kb-router"},
    }
    resp = client.post("/marker", json=body)
    assert resp.status_code == 201
    data = resp.json()
    today = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
    # No matching project dir → falls through to daily/.
    assert data["vault_path"] == f"daily/{today}.md"
    assert "Stream 1A" in (vault / data["vault_path"]).read_text()


def test_marker_milestone_routes_to_project_when_present(vault: Path, client):
    (vault / "left" / "projects" / "phase-7").mkdir(parents=True)
    body = {"type": "milestone", "content": "Stream shipped.", "attrs": {"source": "phase-7"}}
    resp = client.post("/marker", json=body)
    data = resp.json()
    assert data["vault_path"] == "left/projects/phase-7/BLOCKS.md"
    assert (vault / data["vault_path"]).exists()


def test_marker_signal_creates_new_file(vault: Path, client):
    body = {
        "type": "signal",
        "content": "Disk full on host-01.",
        "attrs": {"severity": "critical", "source": "ops"},
    }
    resp = client.post("/marker", json=body)
    assert resp.status_code == 201
    data = resp.json()
    assert data["marker_type"] == "signal"
    assert data["action"] == "created"
    assert data["vault_path"].startswith("amygdala/")
    assert "critical" in data["vault_path"]
    content = (vault / data["vault_path"]).read_text()
    assert "severity: critical" in content


def test_marker_decision_increments_adr_number(vault: Path, client):
    body = {"type": "decision", "content": "Adopt kernel v0.1.0 tag for install pinning."}
    r1 = client.post("/marker", json=body).json()
    r2 = client.post("/marker", json={**body, "attrs": {"title": "second"}}).json()
    assert r1["vault_path"].startswith("left/decisions/ADR-0001-")
    assert r2["vault_path"].startswith("left/decisions/ADR-0002-")


def test_marker_idempotency_replays_cached_response(vault: Path, client):
    body = {"type": "lesson", "content": "Only write this once.", "attrs": {"source": "test"}}
    key = "test-idempotency-1"
    r1 = client.post("/marker", json=body, headers={"X-Idempotency-Key": key}).json()
    r2 = client.post("/marker", json=body, headers={"X-Idempotency-Key": key}).json()
    assert r1["vault_path"] == r2["vault_path"]
    assert r2.get("idempotent_replay") is True
    # File appended exactly once.
    assert (vault / r1["vault_path"]).read_text().count("##") == 1


def test_marker_content_size_limit(client):
    body = {"type": "lesson", "content": "x" * 5000, "attrs": {}}
    resp = client.post("/marker", json=body)
    assert resp.status_code == 400


def test_marker_backdates_from_attrs_ts(vault: Path, client):
    """A replayed marker (outbox sync) lands in its ORIGINAL dated file."""
    body = {
        "type": "lesson",
        "content": "Replayed from the backlog months later.",
        "attrs": {"source": "sync", "ts": "2026-05-12T19:27:10.967339+00:00"},
    }
    resp = client.post("/marker", json=body)
    assert resp.status_code == 201
    assert resp.json()["vault_path"] == "left/reference/lessons-2026-05-12.md"
    written = (vault / "left/reference/lessons-2026-05-12.md").read_text()
    assert "2026-05-12T19:27:10+00:00" in written


def test_marker_unparseable_ts_falls_back_to_now(vault: Path, client):
    body = {
        "type": "lesson",
        "content": "Garbage timestamp must not 500.",
        "attrs": {"source": "sync", "ts": "not-a-date"},
    }
    resp = client.post("/marker", json=body)
    assert resp.status_code == 201
    today = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
    assert resp.json()["vault_path"] == f"left/reference/lessons-{today}.md"


def test_signal_same_second_collision_disambiguates(vault: Path, client):
    """Two DIFFERENT signals in the same second must both persist — a
    filename clash may not masquerade as a rejection (true duplicates are
    already stopped by the idempotency layer upstream)."""
    body = {"type": "signal", "content": "burst one", "attrs": {"title": "auth down"}}
    body2 = {"type": "signal", "content": "burst two", "attrs": {"title": "auth down"}}
    r1 = client.post("/marker", json=body)
    r2 = client.post("/marker", json=body2)
    assert r1.status_code == 201 and r2.status_code == 201
    p1, p2 = r1.json()["vault_path"], r2.json()["vault_path"]
    assert p1 != p2
    assert (vault / p1).exists() and (vault / p2).exists()


def test_decision_same_second_collision_bumps_adr(vault: Path, client):
    body = {"type": "decision", "content": "pick postgres", "attrs": {"title": "same title"}}
    body2 = {"type": "decision", "content": "pick redis", "attrs": {"title": "same title"}}
    r1 = client.post("/marker", json=body)
    r2 = client.post("/marker", json=body2)
    assert r1.status_code == 201 and r2.status_code == 201
    assert r1.json()["vault_path"] != r2.json()["vault_path"]


def test_marker_lesson_seeds_frontmatter(vault: Path, client):
    """A fresh day's log opens with frontmatter.

    Without it the file embeds as `Title: untitled` with no region — which is
    what made lessons indistinguishable from dead session arcs in the index.
    """
    resp = client.post(
        "/marker",
        json={"type": "lesson", "content": "Frontmatter seeding lesson.", "attrs": {}},
    )
    assert resp.status_code == 201
    today = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
    written = (vault / resp.json()["vault_path"]).read_text()
    assert written.startswith("---\n")
    assert f"id: lessons-{today}" in written
    assert "type: lesson-log" in written


def test_marker_lesson_frontmatter_written_once(vault: Path, client):
    """Appending a second lesson must not seed a second frontmatter block."""
    for content in ("First distinct lesson.", "Second distinct lesson."):
        client.post("/marker", json={"type": "lesson", "content": content, "attrs": {}})
    today = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
    written = (vault / f"left/reference/lessons-{today}.md").read_text()
    assert written.count("type: lesson-log") == 1
    assert "First distinct lesson." in written
    assert "Second distinct lesson." in written


def test_marker_lesson_dedupes_repeated_content(vault: Path, client):
    """The same lesson re-emitted later appends once, not twice.

    Different session_id and timestamp, so the HTTP idempotency cache cannot be
    what suppresses the second write — this exercises the on-disk content hash.
    Four copies of one lesson in a single file is the observed failure.
    """
    content = "A refused TCP connection means BUSY, not DOWN."
    first = client.post(
        "/marker",
        json={"type": "lesson", "content": content, "attrs": {"session_id": "s1"}},
    )
    second = client.post(
        "/marker",
        json={"type": "lesson", "content": content, "attrs": {"session_id": "s2"}},
    )
    assert first.json()["action"] == "appended"
    assert second.status_code == 201
    assert second.json()["action"] == "duplicate"

    written = (vault / first.json()["vault_path"]).read_text()
    assert written.count(content) == 1


def test_marker_lesson_distinct_content_still_appends(vault: Path, client):
    """Dedup must not swallow a genuinely different lesson."""
    client.post("/marker", json={"type": "lesson", "content": "Lesson one.", "attrs": {}})
    resp = client.post("/marker", json={"type": "lesson", "content": "Lesson two.", "attrs": {}})
    assert resp.json()["action"] == "appended"
    written = (vault / resp.json()["vault_path"]).read_text()
    assert "Lesson one." in written and "Lesson two." in written


def test_marker_lesson_dedupes_content_containing_a_heading(vault: Path, client):
    """A `## ` line inside the lesson body must not defeat dedup.

    The entry boundary requires an ISO timestamp for exactly this reason.
    """
    content = "Intro line.\n## Not a header, part of the lesson\ntail line."
    first = client.post(
        "/marker", json={"type": "lesson", "content": content, "attrs": {"session_id": "s1"}}
    )
    second = client.post(
        "/marker", json={"type": "lesson", "content": content, "attrs": {"session_id": "s2"}}
    )
    assert first.json()["action"] == "appended"
    assert second.json()["action"] == "duplicate"
    written = (vault / first.json()["vault_path"]).read_text()
    assert written.count("## Not a header, part of the lesson") == 1


def test_marker_lesson_dedupes_crlf_content(vault: Path, client):
    """CRLF content must still dedupe against its own on-disk copy.

    The two sides of the comparison take different routes: the incoming lesson
    is hashed as submitted, while the stored copy has been through read_text,
    whose universal-newline handling rewrites \\r\\n to \\n. Un-normalized, a
    lesson pasted from a Windows source never matches itself.
    """
    content = "line one\r\nline two\r\nline three"
    first = client.post(
        "/marker", json={"type": "lesson", "content": content, "attrs": {"session_id": "s1"}}
    )
    second = client.post(
        "/marker", json={"type": "lesson", "content": content, "attrs": {"session_id": "s2"}}
    )
    assert first.json()["action"] == "appended"
    assert second.json()["action"] == "duplicate"
    assert (vault / first.json()["vault_path"]).read_text().count("line two") == 1


def test_the_same_alarm_re_emitted_later_does_not_create_a_second_file(vault: Path, client):
    """Idempotency is a one-hour cache, so tomorrow's re-emission is a new
    marker — and a signal filename is timestamped, so nothing downstream could
    tell the copy from a fresh incident. One CI failure left seven files this
    way, four of them byte-identical re-emissions of an already-resolved note.
    """
    body = {
        "type": "signal",
        "content": "Branch=dev SHA=6f8c941 Run=31518571119 deploy failed",
        "attrs": {"severity": "nuclear", "source": "github-actions"},
    }
    first = client.post("/marker", json=body, headers={"X-Idempotency-Key": "a"}).json()
    second = client.post("/marker", json=body, headers={"X-Idempotency-Key": "b"}).json()

    assert first["action"] == "created"
    assert second["action"] == "duplicate"
    assert second["vault_path"] == first["vault_path"]
    assert second["written_bytes"] == 0
    assert len(_signal_files(vault)) == 1


def test_a_different_alarm_is_never_absorbed(vault: Path, client):
    a = {"type": "signal", "content": "auth-broker down", "attrs": {"title": "t"}}
    b = {"type": "signal", "content": "postgres down", "attrs": {"title": "t"}}
    r1 = client.post("/marker", json=a, headers={"X-Idempotency-Key": "1"}).json()
    r2 = client.post("/marker", json=b, headers={"X-Idempotency-Key": "2"}).json()
    assert r2["action"] == "created"
    assert r1["vault_path"] != r2["vault_path"]


def test_an_escalation_of_the_same_text_is_not_absorbed(vault: Path, client):
    """The same sentence at a higher severity is a NEW alarm.

    Matching on body alone meant a `nuclear` vanished into an existing
    `warning` file: no file written, nothing broadcast, and a response that
    handed the caller the old path with no way to notice. A dedup that can
    swallow an escalation is worse than no dedup.
    """
    text = "deploy failed"
    warn = client.post(
        "/marker",
        json={"type": "signal", "content": text, "attrs": {"severity": "warning", "source": "ci"}},
        headers={"X-Idempotency-Key": "w"},
    ).json()
    nuke = client.post(
        "/marker",
        json={"type": "signal", "content": text, "attrs": {"severity": "nuclear", "source": "ci"}},
        headers={"X-Idempotency-Key": "n"},
    ).json()

    assert nuke["action"] == "created"
    assert nuke["vault_path"] != warn["vault_path"]
    assert "severity: nuclear" in (vault / nuke["vault_path"]).read_text()


def test_the_same_text_from_a_second_watcher_is_not_absorbed(vault: Path, client):
    """Two independent observers reporting the same condition is corroboration,
    and which watcher saw it is often the whole diagnostic value."""
    text = "deploy failed"
    ci = client.post(
        "/marker",
        json={"type": "signal", "content": text, "attrs": {"severity": "warning", "source": "ci"}},
        headers={"X-Idempotency-Key": "c"},
    ).json()
    cron = client.post(
        "/marker",
        json={
            "type": "signal",
            "content": text,
            "attrs": {"severity": "warning", "source": "cron"},
        },
        headers={"X-Idempotency-Key": "k"},
    ).json()

    assert cron["action"] == "created"
    assert cron["vault_path"] != ci["vault_path"]


def test_a_synthesized_incident_arc_is_not_a_dedup_candidate(vault: Path, client):
    """amygdala/ is heterogeneous. The tick's synthesis phase files incident
    arcs here whose `severity: nuclear` is a synthesis score, not an alarm
    level, and whose schema is entirely different. Scanning them is wasted I/O
    at best and a wrong match at worst."""
    (vault / "amygdala").mkdir(parents=True, exist_ok=True)
    (vault / "amygdala" / "2026-05-15T02-10-09Z-tick-complete.md").write_text(
        "---\ncluster_id: amygdala-tick\nseverity: nuclear\nstatus: active\nheat: 3\n---\n\n"
        "brain tick complete\n"
    )
    resp = client.post(
        "/marker",
        json={
            "type": "signal",
            "content": "brain tick complete",
            "attrs": {"severity": "nuclear", "source": "unknown"},
        },
    ).json()

    assert resp["action"] == "created", "a synthesized arc must not absorb a real alarm"


def test_a_resolved_signal_does_not_suppress_the_condition_firing_again(vault: Path, client):
    """Dedup is scoped to OPEN signals. Once an incident is closed, the same
    condition recurring is news, not a repeat."""
    content = "nightly backup failed on host-01"
    client.post(
        "/marker",
        json={"type": "signal", "content": content, "attrs": {"severity": "resolved"}},
        headers={"X-Idempotency-Key": "r1"},
    )
    again = client.post(
        "/marker",
        json={"type": "signal", "content": content, "attrs": {"severity": "critical"}},
        headers={"X-Idempotency-Key": "r2"},
    ).json()

    assert again["action"] == "created"
    assert len(_signal_files(vault)) == 2


def test_resolved_is_a_writable_severity(vault: Path, client):
    """It was being coerced to `warning`, so the only way a nuclear signal ever
    stopped broadcasting was to outlive its five-day window — for a condition
    often fixed in one minute."""
    resp = client.post(
        "/marker",
        json={
            "type": "signal",
            "content": "run 31518571119 is resolved; fixed in cf4ad749",
            "attrs": {"severity": "resolved", "source": "github-actions"},
        },
    )
    data = resp.json()
    assert "resolved" in data["vault_path"]
    assert "severity: resolved" in (vault / data["vault_path"]).read_text()


def _signal_files(vault: Path) -> list[Path]:
    """Signals only — the scaffold seeds amygdala/README.md as documentation."""
    return [p for p in (vault / "amygdala").glob("*.md") if p.name != "README.md"]
