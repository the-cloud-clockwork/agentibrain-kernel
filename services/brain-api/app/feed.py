"""Feed reader — hot arcs + inject blocks + operator intent.

Reads `$VAULT_ROOT/brain-feed/*.md` (top-level only; `ticks/` subdir skipped)
and parses each file's YAML frontmatter + body. Returns ranked entries for
the `GET /feed` endpoint. This is the HTTP replacement for the filesystem
read path used by `agentihooks/hooks/context/brain_adapter.py::FileBrainSource`.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

VAULT_ROOT = Path(os.environ.get("VAULT_ROOT", "/vault")).resolve()
BRAIN_FEED_DIR = os.environ.get("BRAIN_FEED_DIR", "brain-feed").strip("/")
CLUSTERS_DIR = os.environ.get("CLUSTERS_DIR", "clusters").strip("/")


@dataclass
class FeedEntry:
    id: str
    title: str
    content: str
    priority: int = 5
    ttl: int = 3600
    severity: str = "info"
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "content": self.content,
            "priority": self.priority,
            "ttl": self.ttl,
            "severity": self.severity,
            "metadata": self.metadata,
        }


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """Minimal YAML frontmatter parser — flat key: value only, quote-stripped."""
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    fm: dict[str, Any] = {}
    for line in parts[1].strip().splitlines():
        if ":" in line and not line.lstrip().startswith("#"):
            key, _, val = line.partition(":")
            fm[key.strip()] = val.strip().strip('"').strip("'")
    return fm, parts[2].strip()


def _coerce_int(v: Any, default: int) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def read_feed(vault_root: Path | None = None) -> list[FeedEntry]:
    """Read brain-feed/*.md into FeedEntry list, sorted by priority desc.

    Skips the `ticks/` subdirectory (per-tick run records, not feed content).
    Files without a body are skipped. Files that fail to read are logged and skipped.
    """
    root = Path(vault_root) if vault_root else VAULT_ROOT
    feed_dir = root / BRAIN_FEED_DIR
    if not feed_dir.is_dir():
        return []

    entries: list[FeedEntry] = []
    for md_file in sorted(feed_dir.glob("*.md")):
        try:
            text = md_file.read_text(encoding="utf-8")
        except OSError:
            continue
        # Feed files MUST have YAML frontmatter. Documentation files (README.md
        # etc.) live alongside in the scaffold and must be ignored.
        if not text.startswith("---"):
            continue
        fm, body = _parse_frontmatter(text)
        if not fm or not body.strip():
            continue
        entries.append(
            FeedEntry(
                id=fm.get("id", md_file.stem),
                title=fm.get("title", md_file.stem),
                content=body.strip(),
                priority=_coerce_int(fm.get("priority"), 5),
                ttl=_coerce_int(fm.get("ttl"), 3600),
                severity=fm.get("severity", "info"),
                metadata=fm,
            )
        )
    entries.sort(key=lambda e: e.priority, reverse=True)
    return entries


def feed_payload(vault_root: Path | None = None) -> dict:
    """Build the full /feed response payload."""
    entries = read_feed(vault_root)
    hot_arcs = [
        e.to_dict() for e in entries if e.id.startswith("hot-arcs") or "hot" in e.id.lower()
    ]
    inject_blocks = [e.to_dict() for e in entries if e.id == "inject" or "inject" in e.id.lower()]
    other = [
        e.to_dict()
        for e in entries
        if not (
            e.id.startswith("hot-arcs")
            or e.id == "inject"
            or "hot" in e.id.lower()
            or "inject" in e.id.lower()
        )
    ]
    now = datetime.now(tz=timezone.utc).isoformat(timespec="seconds")
    raw = "|".join(e.id + ":" + str(e.priority) + ":" + e.content[:32] for e in entries)
    return {
        "hot_arcs": hot_arcs,
        "inject_blocks": inject_blocks,
        "entries": other,
        "generated_at": now,
        "hash": hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16],
        "vault_root": str(VAULT_ROOT),
        "entry_count": len(entries),
    }


# Kept in sync with services/brain-ops/project_feed.py, which writes these
# files — different services, no shared import.
PROJECT_SNAPSHOT_DIR = "projects"
PROJECT_INTENT_DIR = "project-intent"
FLEET_WARNINGS_FILE = "fleet-warnings.json"
PROJECT_ITEM_KINDS = ("arcs", "lessons", "decisions", "signals")
PROJECT_FEED_STALE_SECONDS = int(os.getenv("BRAIN_PROJECT_FEED_STALE_SECONDS", "10800"))
PROJECT_INTENT_STALE_SECONDS = int(os.getenv("BRAIN_PROJECT_INTENT_STALE_SECONDS", "21600"))


def project_key(project_id: str) -> str:
    return hashlib.sha256(project_id.encode("utf-8")).hexdigest()[:24]


def _read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _age_seconds(stamp: Any, now: datetime) -> int | None:
    try:
        when = datetime.fromisoformat(str(stamp))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0, int((now - when).total_seconds()))


def _freshness(snapshot: dict | None, checked: float | None, now: datetime) -> dict:
    if snapshot is None or checked is None:
        return {"state": "missing", "generated_at": None, "checked_at": None, "age_seconds": None}
    checked_at = datetime.fromtimestamp(checked, tz=timezone.utc)
    age = max(0, int((now - checked_at).total_seconds()))
    return {
        "state": "stale" if age > PROJECT_FEED_STALE_SECONDS else "fresh",
        "generated_at": snapshot.get("generated_at"),
        "checked_at": checked_at.isoformat(timespec="seconds"),
        "age_seconds": age,
    }


def _intent(record: dict | None, now: datetime) -> dict | None:
    if record is None:
        return None
    age = _age_seconds(record.get("inferred_at"), now)
    fresh = record.get("state") == "fresh" and age is not None
    return {
        "text": str(record.get("text") or ""),
        "label": "inferred",
        "state": "fresh" if fresh and age <= PROJECT_INTENT_STALE_SECONDS else "stale",
        "inferred_at": record.get("inferred_at"),
    }


def _fleet_warnings(feed_dir: Path) -> list[dict]:
    record = _read_json(feed_dir / FLEET_WARNINGS_FILE) or {}
    warnings = record.get("warnings")
    if not isinstance(warnings, list):
        return []
    return [w for w in warnings if isinstance(w, dict) and w.get("scope") == "fleet"]


def project_feed_payload(
    project_id: str, brain_id: str, vault_root: Path | None = None, now: datetime | None = None
) -> dict:
    """One project's own feed: its snapshot, inferred intent and shared fleet warnings.

    A project with no snapshot gets a scoped empty result, never global memory.
    Any item or record naming another project is dropped and counted as a
    cross scope failure.
    """
    root = Path(vault_root) if vault_root else VAULT_ROOT
    feed_dir = root / BRAIN_FEED_DIR
    now = now or datetime.now(tz=timezone.utc)
    key = project_key(project_id)
    failures = 0
    snapshot_path = feed_dir / PROJECT_SNAPSHOT_DIR / f"{key}.json"
    snapshot = _read_json(snapshot_path)
    checked = snapshot_path.stat().st_mtime if snapshot is not None else None
    if snapshot is not None and snapshot.get("project_id") != project_id:
        snapshot, failures = None, failures + 1
    intent_record = _read_json(feed_dir / PROJECT_INTENT_DIR / f"{key}.json")
    if intent_record is not None and intent_record.get("project_id") != project_id:
        intent_record, failures = None, failures + 1
    items: dict[str, list[dict]] = {}
    for kind in PROJECT_ITEM_KINDS:
        rows = (snapshot or {}).get(kind)
        rows = rows if isinstance(rows, list) else []
        kept = [r for r in rows if isinstance(r, dict) and r.get("projects") == [project_id]]
        failures += len(rows) - len(kept)
        items[kind] = kept
    intent = _intent(intent_record, now)
    fleet = _fleet_warnings(feed_dir)
    revision = hashlib.sha256(
        json.dumps({"items": items, "intent": intent, "fleet": fleet}, sort_keys=True).encode()
    ).hexdigest()[:16]
    return {
        "scope": "project",
        "project_id": project_id,
        "brain_id": brain_id,
        "revision": revision,
        "freshness": _freshness(snapshot, checked, now),
        **items,
        "intent": intent,
        "fleet_warnings": fleet,
        "metrics": {"project_feed_cross_scope_failures": failures},
    }
