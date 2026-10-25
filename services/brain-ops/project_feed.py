"""Per-project feed snapshots and inferred project intent.

Each canonical project gets its own ranked arcs, current lessons and decisions,
and signals, chosen within that project before any fleet wide cut, so a quiet
project's cold but active task is never crowded out by a dominant one. Only
signals explicitly marked `scope=fleet` reach the separate fleet record.

File contract with brain-api `app/feed.py` (different services, no shared
import): `brain-feed/projects/<key>.json`, `brain-feed/project-intent/<key>.json`
and `brain-feed/fleet-warnings.json`, where key is the first 24 hex digits of
the sha256 of the canonical project id.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime
from pathlib import Path

import knowledge_index
import markers

SNAPSHOT_DIR = "projects"
INTENT_DIR = "project-intent"
FLEET_FILE = "fleet-warnings.json"
SCHEMA = 1
ITEM_KINDS = ("arcs", "lessons", "decisions", "signals")
ARCS_PER_PROJECT = int(os.getenv("BRAIN_PROJECT_FEED_ARCS", "10"))
KNOWLEDGE_PER_PROJECT = int(os.getenv("BRAIN_PROJECT_FEED_KNOWLEDGE", "10"))
UNATTRIBUTED = frozenset({"", "unknown", "unattributed"})
SEVERITY_RANK = {"warning": 1, "critical": 2, "nuclear": 3}
ALARM_SEVERITIES = frozenset(SEVERITY_RANK)


def project_key(project_id: str) -> str:
    return hashlib.sha256(project_id.encode("utf-8")).hexdigest()[:24]


def _revision(data) -> str:
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def _project(arc: markers.DocumentMeta) -> str:
    project = str(arc.frontmatter.get("project") or "").strip()
    return "" if project.lower() in UNATTRIBUTED else project


def _is_arc(arc: markers.DocumentMeta) -> bool:
    return bool(arc.frontmatter.get("cluster_id"))


def _heat(arc: markers.DocumentMeta) -> int:
    try:
        return int(arc.frontmatter.get("heat", 0))
    except (TypeError, ValueError):
        return 0


def _live_alarm(sig: markers.Marker) -> bool:
    severity = sig.attr("severity", "").strip().lower()
    return (
        severity in ALARM_SEVERITIES
        and sig.attrs.get("_resolved") != "true"
        and sig.attrs.get("_mitigated") != "true"
    )


def _fleet_shared(sig: markers.Marker) -> bool:
    return sig.attr("scope", "").strip().lower() == "fleet"


def _signal_item(sig: markers.Marker, projects: list[str], scope: str) -> dict:
    content = sig.content.strip()
    return {
        "ref": f"signal:{hashlib.sha256(content.encode('utf-8')).hexdigest()[:16]}",
        "projects": projects,
        "scope": scope,
        "severity": sig.attr("severity", "").strip().lower(),
        "source": sig.attr("source", ""),
        "content": content,
    }


def _arc_item(arc: markers.DocumentMeta, project: str) -> dict:
    fm = arc.frontmatter
    return {
        "ref": f"arc:{fm['cluster_id']}",
        "projects": [project],
        "title": str(fm.get("title") or ""),
        "status": str(fm.get("status") or ""),
        "heat": _heat(arc),
        "task_id": str(fm.get("task_id") or ""),
    }


def _knowledge_item(
    item: knowledge_index.KnowledgeItem, project: str, project_arcs: set[str]
) -> dict:
    meta = item.metadata
    return {
        "ref": item.key,
        "projects": [project],
        "content": item.content,
        "entry_ts": meta["source"]["entry_ts"],
        "arcs": [a for a in meta["arcs"] if a in project_arcs],
    }


def _knowledge_by_project(
    vault: Path, arc_ids: dict[str, set[str]]
) -> dict[str, dict[str, list[dict]]]:
    items, _, _ = knowledge_index.scan_items(vault)
    found: dict[str, dict[str, list[dict]]] = {}
    for item in items:
        if item.metadata["status"] != "current":
            continue
        kind = f"{item.kind}s"
        for project in item.metadata["projects"]:
            row = _knowledge_item(item, project, arc_ids.get(project, set()))
            found.setdefault(project, {}).setdefault(kind, []).append(row)
    for kinds in found.values():
        for kind, rows in kinds.items():
            rows.sort(key=lambda r: (r["entry_ts"], r["ref"]), reverse=True)
            kinds[kind] = rows[:KNOWLEDGE_PER_PROJECT]
    return found


def _arcs_by_project(arcs: list[markers.DocumentMeta]) -> dict[str, list[markers.DocumentMeta]]:
    found: dict[str, list[markers.DocumentMeta]] = {}
    for arc in arcs:
        project = _project(arc)
        if project and _is_arc(arc):
            found.setdefault(project, []).append(arc)
    return found


def _signals(project_arcs: list[markers.DocumentMeta], project: str) -> list[dict]:
    rows: dict[str, dict] = {}
    for arc in project_arcs:
        for sig in arc.signals:
            if _live_alarm(sig) and not _fleet_shared(sig):
                item = _signal_item(sig, [project], "project")
                rows.setdefault(item["ref"], item)
    ranked = sorted(rows.values(), key=lambda r: (-SEVERITY_RANK[r["severity"]], r["ref"]))
    return ranked[:KNOWLEDGE_PER_PROJECT]


def build_snapshots(
    arcs: list[markers.DocumentMeta], vault: Path, now: datetime
) -> dict[str, dict]:
    """One snapshot per canonical project, each ranked within its own project."""
    by_project = _arcs_by_project(arcs)
    arc_ids = {
        p: {str(a.frontmatter["cluster_id"]) for a in found} for p, found in by_project.items()
    }
    knowledge = _knowledge_by_project(vault, arc_ids)
    snapshots = {}
    for project in sorted(set(by_project) | set(knowledge)):
        project_arcs = by_project.get(project, [])
        ranked = sorted(
            project_arcs,
            key=lambda a: (
                str(a.frontmatter.get("status", "")).lower() == "active",
                _heat(a),
                str(a.frontmatter["cluster_id"]),
            ),
            reverse=True,
        )[:ARCS_PER_PROJECT]
        items = {
            "arcs": [_arc_item(a, project) for a in ranked],
            "lessons": knowledge.get(project, {}).get("lessons", []),
            "decisions": knowledge.get(project, {}).get("decisions", []),
            "signals": _signals(project_arcs, project),
        }
        snapshots[project] = {
            "schema": SCHEMA,
            "project_id": project,
            "generated_at": now.isoformat(timespec="seconds"),
            "revision": _revision(items),
            "active": any(item["status"].lower() == "active" for item in items["arcs"]),
            **items,
        }
    return snapshots


def fleet_warnings(
    arcs: list[markers.DocumentMeta], now: datetime, vault: Path | None = None
) -> dict:
    rows: dict[str, dict] = {}
    for arc in arcs:
        for sig in arc.signals:
            if _fleet_shared(sig) and _live_alarm(sig):
                item = _signal_item(sig, [], "fleet")
                rows.setdefault(item["ref"], item)
    for item in _posted_fleet_signals(vault) if vault else []:
        rows.setdefault(item["ref"], item)
    warnings = list(rows.values())
    return {
        "schema": SCHEMA,
        "generated_at": now.isoformat(timespec="seconds"),
        "revision": _revision(warnings),
        "warnings": warnings,
    }


def _posted_fleet_signals(vault: Path) -> list[dict]:
    """Signal files brain-api wrote for markers posted with scope=fleet."""
    found = []
    for path in sorted((vault / "amygdala").glob("*.md")):
        try:
            fm, body = markers.parse_frontmatter(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            continue
        severity = str(fm.get("severity", "")).strip().lower()
        if str(fm.get("scope", "")).strip().lower() != "fleet" or severity not in ALARM_SEVERITIES:
            continue
        content = body.strip()
        found.append(
            {
                "ref": f"signal:{hashlib.sha256(content.encode('utf-8')).hexdigest()[:16]}",
                "projects": [],
                "scope": "fleet",
                "severity": severity,
                "source": str(fm.get("source", "")),
                "content": content,
            }
        )
    return found


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(data, indent=1, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def write_snapshots(brain_feed_dir: Path, snapshots: dict[str, dict], fleet: dict) -> dict:
    """Write every changed snapshot, then remove snapshots of projects no longer present.

    A stored snapshot generated after this run is newer and is never replaced
    or removed by it. An unchanged revision keeps its bytes and generated_at;
    only its file time moves, which brain-api reads as the time it was checked.
    """
    out_dir = brain_feed_dir / SNAPSHOT_DIR
    stamp = fleet["generated_at"]
    keep = set()
    stats = {
        "projects_written": 0,
        "projects_unchanged": 0,
        "projects_removed": 0,
        "projects_kept_newer": 0,
    }
    for project, snapshot in snapshots.items():
        name = f"{project_key(project)}.json"
        keep.add(name)
        if _newer(out_dir / name, stamp):
            stats["projects_kept_newer"] += 1
            continue
        stats["projects_written" if _store(out_dir / name, snapshot) else "projects_unchanged"] += 1
    for path in out_dir.glob("*.json") if out_dir.is_dir() else []:
        if path.name not in keep and not _newer(path, stamp):
            path.unlink()
            stats["projects_removed"] += 1
    if not _newer(brain_feed_dir / FLEET_FILE, stamp):
        _store(brain_feed_dir / FLEET_FILE, fleet)
    return stats


def _read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _newer(path: Path, stamp: str) -> bool:
    stored = _read_json(path)
    return bool(stored) and str(stored.get("generated_at", "")) > stamp


def _store(path: Path, data: dict) -> bool:
    stored = _read_json(path)
    if stored and stored.get("revision") == data["revision"]:
        os.utime(path)
        return False
    _write_json(path, data)
    return True


def active_projects(brain_feed_dir: Path) -> list[str]:
    out_dir = brain_feed_dir / SNAPSHOT_DIR
    found = []
    for path in sorted(out_dir.glob("*.json")) if out_dir.is_dir() else []:
        data = _read_json(path)
        if data and data.get("active") and path.stem == project_key(data.get("project_id", "")):
            found.append(data["project_id"])
    return sorted(found)


def write_project_intents(
    brain_feed_dir: Path, intents: dict[str, str], now: datetime, dry_run: bool = False
) -> dict:
    """Label each active project's inferred intent fresh, or keep its last one as stale.

    A project the inference skipped keeps its previous text marked stale; one
    with no previous intent gets none. An inference older than the stored one
    never replaces it.
    """
    stats = {"fresh": 0, "stale": 0, "skipped": 0}
    active = set(active_projects(brain_feed_dir))
    stats["skipped"] += len(set(intents) - active)
    stamp = now.isoformat(timespec="seconds")
    for project in sorted(active):
        path = brain_feed_dir / INTENT_DIR / f"{project_key(project)}.json"
        previous = _read_json(path)
        if previous and previous.get("project_id") != project:
            previous = None
        text = intents.get(project, "").strip()
        if previous and str(previous.get("inferred_at", "")) > stamp:
            stats["skipped"] += 1 if text else 0
            continue
        if text:
            record = {"text": text, "state": "fresh", "inferred_at": stamp}
        elif previous:
            record = {
                "text": previous.get("text", ""),
                "state": "stale",
                "inferred_at": previous.get("inferred_at", ""),
            }
        else:
            continue
        stats[record["state"]] += 1
        if not dry_run:
            _write_json(
                path, {"schema": SCHEMA, "project_id": project, "label": "inferred", **record}
            )
    return stats
