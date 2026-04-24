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

import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


VAULT_ROOT = Path(os.environ.get("VAULT_ROOT", "/vault")).resolve()

_VALID_TYPES = frozenset({"lesson", "milestone", "signal", "decision"})
_VALID_SEVERITIES = frozenset({"nuclear", "critical", "warning", "info"})
_SLUG_RE = re.compile(r"[^a-z0-9]+")
_ADR_PATH_RE = re.compile(r"^ADR-(\d+)-", re.IGNORECASE)


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
    sep = "" if not existing or existing.endswith("\n\n") else ("\n" if existing.endswith("\n") else "\n\n")
    path.write_text(existing + sep + body + ("\n" if not body.endswith("\n") else ""), encoding="utf-8")


def _write_new(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise MarkerError(f"marker target already exists: {path}")
    path.write_text(body + ("\n" if not body.endswith("\n") else ""), encoding="utf-8")


def _format_timestamp_utc() -> tuple[str, str]:
    now = datetime.now(tz=timezone.utc)
    return now.strftime("%Y-%m-%d"), now.strftime("%Y%m%dT%H%M%SZ")


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


def _build_signal_file(
    content: str, attrs: dict[str, Any], ts_iso: str, slug: str
) -> str:
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


def _build_decision_file(
    content: str, attrs: dict[str, Any], ts_iso: str, adr_number: int
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
    date_part, stamp = _format_timestamp_utc()
    ts_iso = datetime.now(tz=timezone.utc).isoformat(timespec="seconds")

    if marker_type == "lesson":
        rel = f"left/reference/lessons-{date_part}.md"
        target = _resolve_inside_vault(rel, root)
        body = _build_lesson_entry(content, attrs, ts_iso)
        _append(target, body)
        action = "appended"
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
        rel = f"amygdala/{stamp}-{severity}-{slug}.md"
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
