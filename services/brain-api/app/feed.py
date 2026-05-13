"""Feed reader — hot arcs + inject blocks + operator intent.

Reads `$VAULT_ROOT/brain-feed/*.md` (top-level only; `ticks/` subdir skipped)
and parses each file's YAML frontmatter + body. Returns ranked entries for
the `GET /feed` endpoint. This is the HTTP replacement for the filesystem
read path used by `agentihooks/hooks/context/brain_adapter.py::FileBrainSource`.
"""

from __future__ import annotations

import hashlib
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
    hot_arcs = [e.to_dict() for e in entries if e.id.startswith("hot-arcs") or "hot" in e.id.lower()]
    inject_blocks = [e.to_dict() for e in entries if e.id == "inject" or "inject" in e.id.lower()]
    other = [
        e.to_dict()
        for e in entries
        if not (e.id.startswith("hot-arcs") or e.id == "inject" or "hot" in e.id.lower() or "inject" in e.id.lower())
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
