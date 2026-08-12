"""Marker writer — route brain markers to the correct vault location.

Four marker types, four destinations:
  lesson    → left/reference/lessons-YYYY-MM-DD.md              (append)
  milestone → left/projects/<source>/BLOCKS.md if source matches (append)
              else daily/YYYY-MM-DD.md                           (append)
  signal    → amygdala/YYYYMMDDTHHMMSS-<severity>-<slug>.md     (new file)
  decision  → left/decisions/ADR-<next-number>-<slug>.md        (new file)

This is the HTTP replacement for the SSH+rsync outbox path used by
`agentihooks/hooks/context/brain_writer_hook.py::_write_to_outbox`.
Idempotency is enforced at the HTTP layer via `X-Idempotency-Key`; this
module focuses on file routing + writing.
"""

from __future__ import annotations

import hashlib
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from .feed import _parse_frontmatter

VAULT_ROOT = Path(os.environ.get("VAULT_ROOT", "/vault")).resolve()

_VALID_TYPES = frozenset({"lesson", "milestone", "signal", "decision"})
# `resolved` is a first-class severity, not an omission: it is how an agent
# closes an alarm it has just fixed. Without it here the value was coerced to
# `warning`, so the only way a nuclear signal ever stopped broadcasting was to
# outlive its window — five days for a condition that was often fixed in one.
# `signal.py` has always accepted it on the read side; the write side had not.
_VALID_SEVERITIES = frozenset({"nuclear", "critical", "warning", "info", "resolved"})
_SLUG_RE = re.compile(r"[^a-z0-9]+")
# Entry boundary inside a lesson log. The timestamp is required: a bare `^## `
# also matches a markdown heading written inside a lesson's own prose, which
# splits one entry in two and makes the dedup below miss. Kept in sync with
# `services/brain-ops/markers.py` LESSON_ENTRY_SPLIT_RE / LESSON_ENTRY_HEADER_RE
# — different services, no shared import.
_LESSON_ENTRY_SPLIT_RE = re.compile(r"(?m)^(?=##\s+\d{4}-\d{2}-\d{2}T)")
_LESSON_ENTRY_HEADER_RE = re.compile(r"^##\s+(\d{4}-\d{2}-\d{2}T\S*)")
_ADR_PATH_RE = re.compile(r"^ADR-(\d+)-", re.IGNORECASE)
# Newest-first cap on the signal dedup scan. Bounds the cost of a single
# marker write against a directory that would otherwise grow without limit;
# a duplicate older than this window is a re-fire worth recording anyway.
SIGNAL_DEDUP_SCAN_MAX = int(os.getenv("SIGNAL_DEDUP_SCAN_MAX", "300"))
# The filename shape this module writes for signals: a UTC stamp, then severity
# and slug. Scoping the dedup scan to it keeps the scan over files whose schema
# is known, and off the synthesized incident arcs that also live in amygdala/ —
# those carry `severity: nuclear` meaning a synthesis score, not an alarm level.
_SIGNAL_FILENAME_RE = re.compile(r"^\d{8}T\d{6}Z-")


class MarkerError(ValueError):
    """Raised on invalid marker payload."""


def _slugify(text: str, max_len: int = 60) -> str:
    s = _SLUG_RE.sub("-", (text or "").lower()).strip("-")
    return (s or "marker")[:max_len]


def _resolve_inside_vault(rel_path: str, vault_root: Path) -> Path:
    p = (vault_root / rel_path.lstrip("/")).resolve()
    try:
        p.relative_to(vault_root)
    except ValueError as exc:
        raise MarkerError(f"path escapes vault root: {rel_path}") from exc
    return p


def _next_adr_number(decisions_dir: Path) -> int:
    if not decisions_dir.is_dir():
        return 1
    max_n = 0
    for child in decisions_dir.iterdir():
        if not child.is_file():
            continue
        match = _ADR_PATH_RE.match(child.name)
        if match:
            try:
                n = int(match.group(1))
                if n > max_n:
                    max_n = n
            except ValueError:
                continue
    return max_n + 1


def _append(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = ""
    if path.exists():
        try:
            existing = path.read_text(encoding="utf-8")
        except OSError:
            existing = ""
    sep = (
        ""
        if not existing or existing.endswith("\n\n")
        else ("\n" if existing.endswith("\n") else "\n\n")
    )
    path.write_text(
        existing + sep + body + ("\n" if not body.endswith("\n") else ""), encoding="utf-8"
    )


def _write_new(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise MarkerError(f"marker target already exists: {path}")
    path.write_text(body + ("\n" if not body.endswith("\n") else ""), encoding="utf-8")


def _format_timestamp_utc(override_ts: str | None = None) -> tuple[str, str, str]:
    """Return (date_part, stamp, ts_iso), backdated when the marker carries one.

    A replayed marker (outbox/backlog sync) sends its original emission time in
    `attrs.ts`; honouring it keeps lessons/milestones in their original dated
    files and arc dating truthful. Anything unparseable falls back to now.
    """
    moment = datetime.now(tz=timezone.utc)
    if override_ts:
        try:
            parsed = datetime.fromisoformat(str(override_ts).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            moment = parsed.astimezone(timezone.utc)
        except ValueError:
            pass
    return (
        moment.strftime("%Y-%m-%d"),
        moment.strftime("%Y%m%dT%H%M%SZ"),
        moment.isoformat(timespec="seconds"),
    )


def _content_hash(text: str) -> str:
    """Repo-standard content hash, matching brain-ops' recipe exactly.

    Newlines are normalized first because the two sides of the dedup comparison
    take different routes: the incoming lesson is hashed as submitted, while the
    on-disk copy has been through `read_text`, whose universal-newline handling
    silently rewrites `\\r\\n` to `\\n`. Without this, a lesson pasted from a
    Windows source never matches itself on resubmission — precisely the repeat
    the dedup exists to stop.
    """
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(normalized.strip().encode("utf-8")).hexdigest()[:16]


def _lesson_frontmatter(date_part: str) -> str:
    """Frontmatter seeded when a day's lesson log is first created.

    Without it these files embed as `Title: untitled` with no region, which is
    indistinguishable from a dead session arc in the index.

    Kept byte-identical to `services/brain-ops/lesson_reconcile.build_frontmatter`.
    The two live in separate services and cannot share an import, so they agree
    by convention — if this changes, that must too, or the tick's reconcile pass
    will rewrite every log on every tick instead of being a no-op.
    """
    return (
        "---\n"
        f"id: lessons-{date_part}\n"
        f"title: Lessons — {date_part}\n"
        "type: lesson-log\n"
        f"created: {date_part}\n"
        "---\n"
    )


def _append_lesson(path: Path, entry: str, content: str, date_part: str) -> str:
    """Append a lesson entry, seeding frontmatter and skipping duplicates.

    Returns "appended" or "duplicate".

    Dedup hashes the lesson *content* only, never the rendered entry: the
    header carries a fresh timestamp and session id every time, so a
    header-inclusive hash matches nothing. Agents re-emit the same lesson
    across sessions, and the HTTP idempotency cache only covers an exact replay
    within its TTL — which is how one log came to hold the same lesson four
    times.

    The index is the target file itself, so no side-car state can drift out of
    sync with the vault, and a legacy file with no frontmatter is handled the
    same as a fresh one. Repetition on a *different* day is left alone: a
    lesson re-learned weeks later is signal.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = ""
    if path.exists():
        try:
            existing = path.read_text(encoding="utf-8")
        except OSError:
            existing = ""

    incoming = _content_hash(content)
    for block in _LESSON_ENTRY_SPLIT_RE.split(existing):
        lines = block.splitlines()
        if not lines or not _LESSON_ENTRY_HEADER_RE.match(lines[0].strip()):
            continue
        block_body = "\n".join(lines[1:]).strip()
        if block_body and _content_hash(block_body) == incoming:
            return "duplicate"

    # Seed frontmatter only when the file is new. Written here rather than via
    # _append, which re-reads from disk and so would drop the seeded header.
    if not existing:
        existing = _lesson_frontmatter(date_part)

    sep = "" if existing.endswith("\n\n") else ("\n" if existing.endswith("\n") else "\n\n")
    path.write_text(
        existing + sep + entry + ("\n" if not entry.endswith("\n") else ""),
        encoding="utf-8",
    )
    return "appended"


def _build_lesson_entry(content: str, attrs: dict[str, Any], ts_iso: str) -> str:
    source = attrs.get("source") or "unknown"
    session_id = attrs.get("session_id") or ""
    header = f"## {ts_iso} — {source}"
    if session_id:
        header += f" — `{session_id}`"
    return f"{header}\n\n{content.strip()}\n"


def _build_milestone_entry(content: str, attrs: dict[str, Any], ts_iso: str) -> str:
    source = attrs.get("source") or "unknown"
    scope = attrs.get("scope") or ""
    status = attrs.get("status") or "done"
    parts = [f"- [x] {ts_iso} — {source}"]
    if scope:
        parts.append(f"scope={scope}")
    if status:
        parts.append(f"status={status}")
    header = " — ".join(parts)
    return f"{header}\n  {content.strip()}\n"


def _build_signal_file(content: str, attrs: dict[str, Any], ts_iso: str, slug: str) -> str:
    severity = (attrs.get("severity") or "warning").lower()
    if severity not in _VALID_SEVERITIES:
        severity = "warning"
    source = attrs.get("source") or "unknown"
    return (
        "---\n"
        f"id: amygdala-{slug}\n"
        f"title: {attrs.get('title') or content[:80].strip()}\n"
        f"severity: {severity}\n"
        f"source: {source}\n"
        f"created: {ts_iso}\n"
        "---\n\n"
        f"{content.strip()}\n"
    )


def _existing_signal_with_same_body(
    amygdala_dir: Path, content: str, severity: str, source: str
) -> Path | None:
    """An open signal already carrying this exact claim, if one exists.

    The HTTP idempotency cache has a one-hour TTL, so an agent re-emitting the
    same alarm in tomorrow's session writes a brand-new file — and because a
    signal filename is timestamped (with a uuid suffix on collision, so an alert
    burst never loses a distinct alarm), nothing downstream could tell the copy
    from a fresh incident. One CI failure produced seven files this way, four of
    them byte-identical re-emissions of the same already-resolved note.

    Identity is (source, severity, body), not body alone. The same sentence at a
    higher severity is an **escalation**, and the same sentence from a different
    watcher is corroboration from an independent observer — absorbing either one
    silently destroys an alarm. Matching on body alone meant a `nuclear` from
    cron vanished into an existing `warning` from ci and never broadcast at all.

    Scoped to *open* signals: a resolved one must not suppress the same
    condition genuinely firing again later.

    Candidates are ordered by filename rather than mtime. The names this writer
    produces are UTC timestamps, so lexical order *is* chronological — and it
    costs one readdir, where sorting on mtime stats every file in the directory
    before the cap can apply. Over an NFS mount that is a network round trip
    each. The same pattern scopes the scan to files this code wrote, so the
    synthesized incident arcs that also live here are never read.
    """
    if not amygdala_dir.is_dir():
        return None
    incoming = _content_hash(content)
    want_severity = (severity or "").strip().lower()
    want_source = (source or "").strip().lower()
    try:
        candidates = sorted(
            (
                p
                for p in amygdala_dir.iterdir()
                if p.name.endswith(".md") and _SIGNAL_FILENAME_RE.match(p.name)
            ),
            key=lambda p: p.name,
            reverse=True,
        )[:SIGNAL_DEDUP_SCAN_MAX]
    except OSError:
        return None
    for path in candidates:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError, IsADirectoryError):
            continue
        fm, body = _parse_frontmatter(text)
        existing_severity = str(fm.get("severity", "")).strip().lower()
        if existing_severity == "resolved" or existing_severity != want_severity:
            continue
        if str(fm.get("source", "")).strip().lower() != want_source:
            continue
        if body and _content_hash(body) == incoming:
            return path
    return None


def _build_decision_file(content: str, attrs: dict[str, Any], ts_iso: str, adr_number: int) -> str:
    title = (attrs.get("title") or content[:80].strip()) or f"ADR {adr_number}"
    source = attrs.get("source") or "unknown"
    return (
        "---\n"
        f"id: ADR-{adr_number:04d}\n"
        f"title: {title}\n"
        f"created: {ts_iso}\n"
        f"source: {source}\n"
        "status: proposed\n"
        "---\n\n"
        f"# ADR-{adr_number:04d} — {title}\n\n"
        f"{content.strip()}\n"
    )


def write_marker(
    marker_type: str,
    content: str,
    attrs: dict[str, Any] | None = None,
    vault_root: Path | None = None,
) -> dict:
    """Route a single marker to its vault destination.

    Returns {vault_path, action, marker_type, written_bytes}.
    """
    if marker_type not in _VALID_TYPES:
        raise MarkerError(f"invalid marker type: {marker_type!r}")
    content = (content or "").strip()
    if not content:
        raise MarkerError("content is required")
    if len(content) > 4096:
        raise MarkerError("content exceeds 4KB limit")
    attrs = attrs or {}
    root = Path(vault_root) if vault_root else VAULT_ROOT
    root.mkdir(parents=True, exist_ok=True)
    date_part, stamp, ts_iso = _format_timestamp_utc(attrs.get("ts"))

    if marker_type == "lesson":
        rel = f"left/reference/lessons-{date_part}.md"
        target = _resolve_inside_vault(rel, root)
        body = _build_lesson_entry(content, attrs, ts_iso)
        action = _append_lesson(target, body, content, date_part)
    elif marker_type == "milestone":
        source = attrs.get("source") or ""
        project_rel = ""
        if source:
            project_slug = _slugify(source, max_len=80)
            candidate = root / "left" / "projects" / project_slug / "BLOCKS.md"
            if candidate.parent.is_dir():
                project_rel = f"left/projects/{project_slug}/BLOCKS.md"
        if project_rel:
            rel = project_rel
        else:
            rel = f"daily/{date_part}.md"
        target = _resolve_inside_vault(rel, root)
        body = _build_milestone_entry(content, attrs, ts_iso)
        _append(target, body)
        action = "appended"
    elif marker_type == "signal":
        severity = (attrs.get("severity") or "warning").lower()
        if severity not in _VALID_SEVERITIES:
            severity = "warning"
        slug = _slugify(attrs.get("title") or content, max_len=60)
        # An open signal already carrying this exact claim absorbs the write.
        # Returning the existing path rather than 409-ing keeps the caller's
        # contract unchanged — a re-emission is a no-op, not an error.
        duplicate = _existing_signal_with_same_body(
            root / "amygdala", content, severity, attrs.get("source") or "unknown"
        )
        if duplicate is not None:
            return {
                "vault_path": str(duplicate.relative_to(root)),
                "action": "duplicate",
                "marker_type": marker_type,
                "written_bytes": 0,
            }
        rel = f"amygdala/{stamp}-{severity}-{slug}.md"
        target = _resolve_inside_vault(rel, root)
        # Second-resolution stamps collide under alert bursts (crash loop
        # firing same-title signals within one second). A DIFFERENT signal
        # must never be refused for a filename clash — true duplicates are
        # already caught by the HTTP idempotency layer before reaching here.
        if target.exists():
            rel = f"amygdala/{stamp}-{severity}-{slug}-{uuid4().hex[:6]}.md"
            target = _resolve_inside_vault(rel, root)
        body = _build_signal_file(content, attrs, ts_iso, slug)
        _write_new(target, body)
        action = "created"
    elif marker_type == "decision":
        decisions_dir = root / "left" / "decisions"
        adr_number = _next_adr_number(decisions_dir)
        slug = _slugify(attrs.get("title") or content, max_len=60)
        rel = f"left/decisions/ADR-{adr_number:04d}-{slug}.md"
        target = _resolve_inside_vault(rel, root)
        if target.exists():
            adr_number += 1
            rel = f"left/decisions/ADR-{adr_number:04d}-{slug}.md"
            target = _resolve_inside_vault(rel, root)
        body = _build_decision_file(content, attrs, ts_iso, adr_number)
        _write_new(target, body)
        action = "created"
    else:  # pragma: no cover — guarded above
        raise MarkerError(f"unsupported marker type: {marker_type!r}")

    try:
        written_bytes = target.stat().st_size
    except OSError:
        written_bytes = 0

    return {
        "vault_path": str(target.relative_to(root)),
        "action": action,
        "marker_type": marker_type,
        "written_bytes": written_bytes,
    }
