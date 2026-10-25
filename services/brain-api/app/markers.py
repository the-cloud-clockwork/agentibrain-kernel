"""Marker writer — route brain markers to the correct vault location.

Four marker types, four destinations:
  lesson    → left/reference/lessons-YYYY-MM-DD.md              (append)
  milestone → left/projects/<project>/BLOCKS.md if project matches (append)
              else daily/YYYY-MM-DD.md                           (append)
  signal    → amygdala/YYYYMMDDTHHMMSS-<severity>-<slug>.md     (new file)
  decision  → left/decisions/ADR-<next-number>-<slug>.md        (new file)

This is the HTTP replacement for the SSH+rsync outbox path used by
`agentihooks/hooks/context/brain_writer_hook.py::_write_to_outbox`.
Every marker carries its scope and evidence (`marker_provenance`): frontmatter
for signal and decision files, a `@provenance` comment on the entry header for
lesson and milestone logs. A registry under `brain-feed/markers/`, keyed by the
marker id the HTTP layer derives from `X-Idempotency-Key`, makes a write
happen at most once across processes.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
from collections.abc import Iterator
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
PROVENANCE_FIELDS = (
    "project_id",
    "projects",
    "project",
    "repo",
    "worktree",
    "cwd",
    "remote",
    "branch",
    "session_id",
    "execution",
    "installation",
    "harness",
    "swarm",
    "task",
    "task_revision",
    "lane",
    "attribution",
)
_STORED_KEYS = ("marker_id", "brain", "source_ts", *PROVENANCE_FIELDS)
# Values ride inside an HTML comment or a frontmatter line, so anything that
# could close either, or that the frontmatter reader strips, is unrepresentable
# and counted as lost instead of written.
_UNSAFE_VALUE_RE = re.compile(r"[\"'<>\r\n]|--")
_PROVENANCE_RE = re.compile(r'<!--\s*@provenance((?:\s+\w+="[^"]*")*)\s*-->')
_PROVENANCE_ATTR_RE = re.compile(r'(\w+)="([^"]*)"')
MARKER_REGISTRY_DIR = "brain-feed/markers"
_MARKER_ID_RE = re.compile(r"^mk-[0-9a-f]{32}$")


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


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _format_timestamp_utc(override_ts: str | None = None) -> tuple[str, str, str]:
    """Return (date_part, stamp, ts_iso), backdated when the marker carries one.

    A replayed marker (outbox/backlog sync) sends its original emission time in
    `attrs.ts`; honouring it keeps lessons/milestones in their original dated
    files and arc dating truthful. Anything unparseable falls back to now.
    """
    moment = _parse_ts(override_ts) or datetime.now(tz=timezone.utc)
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


def marker_id_for(idempotency_key: str) -> str:
    return "mk-" + hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:32]


def _listed(value: Any) -> list[str]:
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    if isinstance(value, (list, tuple)):
        return [str(part).strip() for part in value if str(part).strip()]
    return []


def claimed_projects(attrs: dict[str, Any]) -> set[str]:
    claimed = set(_listed(attrs.get("projects")))
    project_id = str(attrs.get("project_id") or "").strip()
    if project_id:
        claimed.add(project_id)
    return claimed - {"unknown"}


def _provenance_value(value: Any) -> str | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (str, int, float)):
        text = str(value).strip()
    elif isinstance(value, (list, tuple)) and all(isinstance(v, str) for v in value):
        text = ",".join(v.strip() for v in value if v.strip())
    else:
        return None
    if _UNSAFE_VALUE_RE.search(text):
        return None
    return text


def marker_provenance(
    attrs: dict[str, Any], marker_id: str, brain: str
) -> tuple[dict[str, str], int]:
    """The scope and evidence fields to persist, and how many supplied ones could not be."""
    stored = {"marker_id": marker_id}
    if brain:
        stored["brain"] = brain
    source = _parse_ts(attrs.get("ts"))
    if source is not None:
        stored["source_ts"] = source.isoformat(timespec="seconds")
    lost = 0
    for name in PROVENANCE_FIELDS:
        raw = attrs.get(name)
        if raw is None or (isinstance(raw, (str, list, tuple)) and not _listed(raw)):
            continue
        value = _provenance_value(raw)
        if value:
            stored[name] = value
        else:
            lost += 1
    return stored, lost


def _provenance_comment(provenance: dict[str, str]) -> str:
    pairs = " ".join(f'{key}="{value}"' for key, value in provenance.items())
    return f"<!-- @provenance {pairs} -->"


def _provenance_frontmatter(provenance: dict[str, str]) -> str:
    return "".join(f"{key}: {value}\n" for key, value in provenance.items())


def _provenance_in(text: str, marker_id: str) -> dict[str, str] | None:
    fm, _ = _parse_frontmatter(text)
    if fm.get("marker_id") == marker_id:
        return {key: fm[key] for key in _STORED_KEYS if fm.get(key)}
    for match in _PROVENANCE_RE.finditer(text):
        found = dict(_PROVENANCE_ATTR_RE.findall(match.group(1)))
        if found.get("marker_id") == marker_id:
            return found
    return None


def _first_marker_id(text: str) -> str:
    fm, _ = _parse_frontmatter(text)
    if fm.get("marker_id"):
        return fm["marker_id"]
    match = _PROVENANCE_RE.search(text)
    return dict(_PROVENANCE_ATTR_RE.findall(match.group(1))).get("marker_id", "") if match else ""


@contextlib.contextmanager
def _registry_lock(root: Path) -> Iterator[Path]:
    """Exclusive across threads and processes; across hosts only where the mount honours flock."""
    registry = root / MARKER_REGISTRY_DIR
    registry.mkdir(parents=True, exist_ok=True)
    with open(registry / ".lock", "a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield registry
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _registry_entry(registry: Path, marker_id: str) -> dict | None:
    if not _MARKER_ID_RE.match(marker_id):
        return None
    try:
        return json.loads((registry / f"{marker_id}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _record(registry: Path, entry: dict) -> None:
    target = registry / f"{entry['marker_id']}.json"
    tmp = registry / f".{entry['marker_id']}.{uuid4().hex}.tmp"
    tmp.write_text(json.dumps(entry, sort_keys=True), encoding="utf-8")
    os.replace(tmp, target)


def read_marker(marker_id: str, vault_root: Path | None = None) -> dict | None:
    """A recorded marker with the provenance read back from its vault file."""
    root = Path(vault_root) if vault_root else VAULT_ROOT
    entry = _registry_entry(root / MARKER_REGISTRY_DIR, marker_id)
    if entry is None or entry.get("state") == "pending":
        return None
    canonical = entry.get("canonical_marker_id") or marker_id
    try:
        target = _resolve_inside_vault(entry["vault_path"], root)
        text = target.read_text(encoding="utf-8")
    except (OSError, MarkerError, KeyError):
        text = ""
    return {
        "marker_id": marker_id,
        "canonical_marker_id": canonical,
        "marker_type": entry.get("marker_type", ""),
        "vault_path": entry.get("vault_path", ""),
        "provenance": _provenance_in(text, canonical) or {},
    }


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


def _append_lesson(path: Path, entry: str, content: str, date_part: str) -> tuple[str, str]:
    """Append a lesson entry, seeding frontmatter and skipping duplicates.

    Returns ("appended", "") or ("duplicate", marker id of the entry kept).

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
            return "duplicate", _first_marker_id(lines[0])

    # Seed frontmatter only when the file is new. Written here rather than via
    # _append, which re-reads from disk and so would drop the seeded header.
    if not existing:
        existing = _lesson_frontmatter(date_part)

    sep = "" if existing.endswith("\n\n") else ("\n" if existing.endswith("\n") else "\n\n")
    path.write_text(
        existing + sep + entry + ("\n" if not entry.endswith("\n") else ""),
        encoding="utf-8",
    )
    return "appended", ""


def _build_lesson_entry(
    content: str, attrs: dict[str, Any], ts_iso: str, provenance: dict[str, str]
) -> str:
    source = attrs.get("source") or "unknown"
    session_id = attrs.get("session_id") or ""
    header = f"## {ts_iso} — {source}"
    if session_id:
        header += f" — `{session_id}`"
    # On the header line, because the entry body is what dedup and reconcile hash.
    header += f" {_provenance_comment(provenance)}"
    return f"{header}\n\n{content.strip()}\n"


def _build_milestone_entry(
    content: str, attrs: dict[str, Any], ts_iso: str, provenance: dict[str, str]
) -> str:
    source = attrs.get("source") or "unknown"
    scope = attrs.get("scope") or ""
    status = attrs.get("status") or "done"
    parts = [f"- [x] {ts_iso} — {source}"]
    if scope:
        parts.append(f"scope={scope}")
    if status:
        parts.append(f"status={status}")
    header = " — ".join(parts) + f" {_provenance_comment(provenance)}"
    return f"{header}\n  {content.strip()}\n"


def _fleet_scoped(attrs: dict[str, Any]) -> bool:
    return str(attrs.get("scope") or "").strip().lower() == "fleet"


def _build_signal_file(
    content: str, attrs: dict[str, Any], ts_iso: str, slug: str, provenance: dict[str, str]
) -> str:
    severity = (attrs.get("severity") or "warning").lower()
    if severity not in _VALID_SEVERITIES:
        severity = "warning"
    source = attrs.get("source") or "unknown"
    scope_line = "scope: fleet\n" if _fleet_scoped(attrs) else ""
    return (
        "---\n"
        f"id: amygdala-{slug}\n"
        f"title: {attrs.get('title') or content[:80].strip()}\n"
        f"severity: {severity}\n"
        f"source: {source}\n"
        f"{scope_line}"
        f"created: {ts_iso}\n"
        f"{_provenance_frontmatter(provenance)}"
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


def _build_decision_file(
    content: str, attrs: dict[str, Any], ts_iso: str, adr_number: int, provenance: dict[str, str]
) -> str:
    title = (attrs.get("title") or content[:80].strip()) or f"ADR {adr_number}"
    source = attrs.get("source") or "unknown"
    return (
        "---\n"
        f"id: ADR-{adr_number:04d}\n"
        f"title: {title}\n"
        f"created: {ts_iso}\n"
        f"source: {source}\n"
        "status: proposed\n"
        f"{_provenance_frontmatter(provenance)}"
        "---\n\n"
        f"# ADR-{adr_number:04d} — {title}\n\n"
        f"{content.strip()}\n"
    )


def _write_lesson(root, content, attrs, stamps, provenance) -> tuple[Path, str, str]:
    date_part, _, ts_iso = stamps
    target = _resolve_inside_vault(f"left/reference/lessons-{date_part}.md", root)
    body = _build_lesson_entry(content, attrs, ts_iso, provenance)
    action, kept = _append_lesson(target, body, content, date_part)
    return target, action, kept


def milestone_project(attrs: dict[str, Any]) -> str:
    project = str(attrs.get("project") or "").strip()
    if project:
        return project
    project_id = str(attrs.get("project_id") or "").strip()
    if project_id and project_id != "unknown":
        return project_id.rsplit("/", 1)[-1]
    # A client older than project attribution only ever named the project in source.
    return str(attrs.get("source") or "").strip()


def granted_names(grant: frozenset[str]) -> set[str]:
    return {project_id.rsplit("/", 1)[-1] for project_id in grant}


def _milestone_target(root, attrs, stamps, grant: frozenset[str]) -> Path:
    rel = f"daily/{stamps[0]}.md"
    project = milestone_project(attrs)
    if project and (not grant or project in granted_names(grant)):
        project_slug = _slugify(project, max_len=80)
        if (root / "left" / "projects" / project_slug).is_dir():
            rel = f"left/projects/{project_slug}/BLOCKS.md"
    return _resolve_inside_vault(rel, root)


def _write_milestone(
    root, content, attrs, stamps, provenance, grant: frozenset[str] = frozenset()
) -> tuple[Path, str, str]:
    target = _milestone_target(root, attrs, stamps, grant)
    _append(target, _build_milestone_entry(content, attrs, stamps[2], provenance))
    return target, "appended", ""


def _write_signal(root, content, attrs, stamps, provenance) -> tuple[Path, str, str]:
    _, stamp, ts_iso = stamps
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
        return duplicate, "duplicate", _first_marker_id(duplicate.read_text(encoding="utf-8"))
    target = _resolve_inside_vault(f"amygdala/{stamp}-{severity}-{slug}.md", root)
    # Second-resolution stamps collide under alert bursts (crash loop
    # firing same-title signals within one second). A DIFFERENT signal
    # must never be refused for a filename clash — true duplicates are
    # already caught by the HTTP idempotency layer before reaching here.
    if target.exists():
        rel = f"amygdala/{stamp}-{severity}-{slug}-{uuid4().hex[:6]}.md"
        target = _resolve_inside_vault(rel, root)
    _write_new(target, _build_signal_file(content, attrs, ts_iso, slug, provenance))
    return target, "created", ""


def _write_decision(root, content, attrs, stamps, provenance) -> tuple[Path, str, str]:
    ts_iso = stamps[2]
    adr_number = _next_adr_number(root / "left" / "decisions")
    slug = _slugify(attrs.get("title") or content, max_len=60)
    target = _resolve_inside_vault(f"left/decisions/ADR-{adr_number:04d}-{slug}.md", root)
    if target.exists():
        adr_number += 1
        target = _resolve_inside_vault(f"left/decisions/ADR-{adr_number:04d}-{slug}.md", root)
    _write_new(target, _build_decision_file(content, attrs, ts_iso, adr_number, provenance))
    return target, "created", ""


def _written_candidates(root, marker_type, attrs, stamps, grant) -> list[Path]:
    if marker_type == "lesson":
        return [root / "left" / "reference" / f"lessons-{stamps[0]}.md"]
    if marker_type == "milestone":
        return [_milestone_target(root, attrs, stamps, grant)]
    if marker_type == "signal":
        folder = root / "amygdala"
        names = sorted(
            (p for p in folder.glob("*.md") if _SIGNAL_FILENAME_RE.match(p.name)),
            key=lambda p: p.name,
            reverse=True,
        )
        return names[:SIGNAL_DEDUP_SCAN_MAX]
    return sorted((root / "left" / "decisions").glob("ADR-*.md"), reverse=True)


def _already_written(root, marker_type, marker_id, attrs, stamps, grant, pending) -> Path | None:
    """A file holding this marker id from an attempt that died between pending and recorded."""
    # The pending entry names the dated log the dead attempt chose, which a retry
    # after midnight would no longer compute.
    noted = [root / rel for rel in pending.get("candidates", [])]
    for path in noted + _written_candidates(root, marker_type, attrs, stamps, grant):
        try:
            if marker_id in path.read_text(encoding="utf-8"):
                return path
        except OSError:
            continue
    return None


def _committed_entry(registry, root, marker_type, marker_id, attrs, stamps, grant) -> dict | None:
    """The recorded write of this marker id, completing one a crash left pending."""
    recorded = _registry_entry(registry, marker_id)
    if recorded is None or recorded.get("state") != "pending":
        return recorded
    orphan = _already_written(root, marker_type, marker_id, attrs, stamps, grant, recorded)
    if orphan is None:
        return None
    recorded = {
        "marker_id": marker_id,
        "canonical_marker_id": marker_id,
        "marker_type": marker_type,
        "vault_path": str(orphan.relative_to(root)),
        "action": "recovered",
    }
    _record(registry, recorded)
    return recorded


_WRITERS = {
    "lesson": _write_lesson,
    "milestone": _write_milestone,
    "signal": _write_signal,
    "decision": _write_decision,
}


def write_marker(
    marker_type: str,
    content: str,
    attrs: dict[str, Any] | None = None,
    vault_root: Path | None = None,
    marker_id: str | None = None,
    brain: str = "",
    grant: frozenset[str] = frozenset(),
) -> dict:
    """Route a single marker to its vault destination, at most once per marker id.

    Returns {vault_path, action, marker_type, written_bytes, marker_id,
    marker_provenance_loss_total}; a marker id already recorded, or already
    in the vault from an attempt that died before recording it, returns
    action "replayed" with the original vault path and writes nothing. A
    non-empty grant limits milestone routing to the granted projects.
    """
    if marker_type not in _VALID_TYPES:
        raise MarkerError(f"invalid marker type: {marker_type!r}")
    content = (content or "").strip()
    if not content:
        raise MarkerError("content is required")
    if len(content) > 4096:
        raise MarkerError("content exceeds 4KB limit")
    attrs = attrs or {}
    marker_id = marker_id or marker_id_for(uuid4().hex)
    if not _MARKER_ID_RE.match(marker_id):
        raise MarkerError("invalid marker id")
    root = Path(vault_root) if vault_root else VAULT_ROOT
    root.mkdir(parents=True, exist_ok=True)
    stamps = _format_timestamp_utc(attrs.get("ts"))
    provenance, lost = marker_provenance(attrs, marker_id, brain)

    with _registry_lock(root) as registry:
        recorded = _committed_entry(registry, root, marker_type, marker_id, attrs, stamps, grant)
        if recorded is not None:
            return {
                "vault_path": recorded["vault_path"],
                "action": "replayed",
                "marker_type": recorded["marker_type"],
                "written_bytes": 0,
                "marker_id": marker_id,
                "marker_provenance_loss_total": 0,
            }
        pending = {"marker_id": marker_id, "marker_type": marker_type, "state": "pending"}
        if marker_type in ("lesson", "milestone"):
            dated = _written_candidates(root, marker_type, attrs, stamps, grant)
            pending["candidates"] = [str(path.relative_to(root)) for path in dated]
        _record(registry, pending)
        extra = {"grant": grant} if marker_type == "milestone" else {}
        target, action, kept = _WRITERS[marker_type](
            root, content, attrs, stamps, provenance, **extra
        )
        vault_path = str(target.relative_to(root))
        _record(
            registry,
            {
                "marker_id": marker_id,
                "canonical_marker_id": kept or marker_id,
                "marker_type": marker_type,
                "vault_path": vault_path,
                "action": action,
            },
        )

    try:
        written_bytes = (
            0 if marker_type == "signal" and action == "duplicate" else target.stat().st_size
        )
    except OSError:
        written_bytes = 0

    return {
        "vault_path": vault_path,
        "action": action,
        "marker_type": marker_type,
        "written_bytes": written_bytes,
        "marker_id": marker_id,
        "marker_provenance_loss_total": lost,
    }
