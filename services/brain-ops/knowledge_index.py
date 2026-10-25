"""Index each lesson entry and decision as its own scoped knowledge item.

A daily lesson log holds many entries from many projects; embedding the log as
one document truncates it and loses each entry's own scope. Every lesson entry
and every decision file here becomes one `brain-knowledge` row whose metadata
carries its canonical project, project set, brain, arc links, provenance and
current revision. A metadata change alone (a decision superseded) is written
through /metadata without re-embedding the content.
"""

from __future__ import annotations

import hashlib
import json
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import markers

PRODUCER = "brain-knowledge"
REPORT_FILENAME = ".brain-knowledge-scope-report.json"
STATE_FILENAME = ".brain-knowledge-embed.state.json"
UNKNOWN = "unknown"
REQ_TIMEOUT = 30


@dataclass(frozen=True)
class KnowledgeItem:
    key: str
    kind: str
    content: str
    metadata: dict


def content_revision(text: str) -> str:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(normalized.strip().encode("utf-8")).hexdigest()[:16]


def _listed(value) -> list[str]:
    if isinstance(value, list):
        parts = value
    else:
        parts = str(value or "").split(",")
    return [p.strip() for p in parts if p and p.strip()]


def _scope(fields: dict) -> tuple[str, list[str]]:
    project_id = str(fields.get("project_id") or "").strip()
    projects = set(_listed(fields.get("projects")))
    if project_id and project_id != UNKNOWN:
        projects.add(project_id)
    projects.discard(UNKNOWN)
    if not project_id or project_id == UNKNOWN:
        project_id = next(iter(projects)) if len(projects) == 1 else UNKNOWN
    return project_id, sorted(projects)


def _metadata(kind: str, fields: dict, body: str, source: dict, status: dict) -> dict:
    project_id, projects = _scope(fields)
    arcs = sorted(
        {m.attr("target") for m in markers.find_markers(body, "edge") if m.attr("target")}
    )
    return {
        "kind": kind,
        "project_id": project_id,
        "projects": projects,
        "brain": str(fields.get("brain") or ""),
        "scope": "project" if projects else "unattributed",
        "arcs": arcs,
        "status": status["status"],
        "superseded_by": status["superseded_by"],
        "revision": content_revision(body),
        "source": source,
    }


def lesson_items(rel: str, text: str) -> list[KnowledgeItem]:
    _, body = markers.parse_frontmatter(text)
    stem = Path(rel).stem
    items = []
    for block in markers.LESSON_ENTRY_SPLIT_RE.split(body):
        lines = block.strip("\n").splitlines()
        header = markers.LESSON_ENTRY_HEADER_RE.match(lines[0].strip()) if lines else None
        if header is None:
            continue
        entry = "\n".join(lines[1:]).strip()
        if not entry:
            continue
        found = markers.find_markers(lines[0], "provenance")
        prov = found[0].attrs if found else {}
        marker_id = prov.get("marker_id", "")
        source = {
            "path": rel,
            "entry_ts": header.group(1),
            "marker_id": marker_id,
            "session_id": prov.get("session_id", ""),
        }
        status = {"status": "current", "superseded_by": ""}
        key = f"lesson:{marker_id}" if marker_id else f"lesson:{stem}:{content_revision(entry)}"
        items.append(
            KnowledgeItem(key, "lesson", entry, _metadata("lesson", prov, entry, source, status))
        )
    return items


def decision_item(rel: str, text: str) -> KnowledgeItem | None:
    fm, body = markers.parse_frontmatter(text)
    body = body.strip()
    if not body:
        return None
    marker_id = str(fm.get("marker_id") or "")
    superseded_by = str(fm.get("superseded_by") or "")
    superseded = superseded_by or str(fm.get("status") or "").lower() == "superseded"
    status = {"status": "superseded" if superseded else "current", "superseded_by": superseded_by}
    source = {
        "path": rel,
        "entry_ts": str(fm.get("created") or ""),
        "marker_id": marker_id,
        "session_id": str(fm.get("session_id") or ""),
    }
    key = f"decision:{marker_id or fm.get('id') or Path(rel).stem}"
    return KnowledgeItem(key, "decision", body, _metadata("decision", fm, body, source, status))


def scan_items(vault: Path) -> tuple[list[KnowledgeItem], list[str]]:
    """Every knowledge item in the vault, and the files that could not be read."""
    items: list[KnowledgeItem] = []
    errors: list[str] = []
    sources = [
        (lesson_items, sorted((vault / "left" / "reference").glob("lessons-*.md"))),
        (decision_item, sorted((vault / "left" / "decisions").glob("*.md"))),
    ]
    for parse, paths in sources:
        for path in paths:
            if parse is lesson_items and not markers.LESSON_LOG_RE.match(path.name):
                continue
            rel = str(path.relative_to(vault))
            try:
                text = path.read_text(encoding="utf-8")
            except OSError as exc:
                print(f"WARN: cannot read {path}: {exc}", file=sys.stderr)
                errors.append(rel)
                continue
            found = parse(rel, text)
            items.extend(found if isinstance(found, list) else [found] if found else [])
    unique = {}
    for item in items:
        unique.setdefault(item.key, item)
    return list(unique.values()), errors


def _post(api_url: str, api_key: str, path: str, payload: dict) -> dict:
    req = urllib.request.Request(
        url=f"{api_url.rstrip('/')}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=REQ_TIMEOUT) as resp:
        return json.loads(resp.read())


def _metadata_hash(metadata: dict) -> str:
    return content_revision(json.dumps(metadata, sort_keys=True))


def _write(api_url: str, api_key: str, item: KnowledgeItem, content_known: bool) -> str:
    if content_known:
        try:
            _post(api_url, api_key, "/metadata", {"key": item.key, "metadata": item.metadata})
            return "metadata"
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                raise
    _post(
        api_url,
        api_key,
        "/embed",
        {
            "key": item.key,
            "content": item.content,
            "producer": PRODUCER,
            "content_type": item.kind,
            "metadata": item.metadata,
        },
    )
    return "embed"


@dataclass
class IndexRun:
    vault: Path
    api_url: str
    api_key: str
    state: dict
    save: Callable[[dict], None]
    report_path: Path | None = None
    dry_run: bool = False
    prune: bool = False


def index_knowledge(run: IndexRun) -> dict:
    """Write changed items, one state save per write, so a failed run resumes where it stopped."""
    items, read_errors = scan_items(run.vault)
    stats = {
        "knowledge_scanned": len(items),
        "knowledge_embedded": 0,
        "knowledge_metadata_updated": 0,
        "knowledge_skipped_unchanged": 0,
        "knowledge_unattributed": sum(i.metadata["project_id"] == UNKNOWN for i in items),
        "knowledge_read_errors": len(read_errors),
        "errors": len(read_errors),
    }
    pending = []
    for item in items:
        prior = run.state.get(item.key) or {}
        if prior.get("metadata") == _metadata_hash(item.metadata):
            stats["knowledge_skipped_unchanged"] += 1
        else:
            pending.append((item, prior.get("revision") == item.metadata["revision"]))
    written = 0
    for item, content_known in pending:
        if run.dry_run:
            print(f"DRY[knowledge]: {item.key} project={item.metadata['project_id']}")
            continue
        try:
            action = _write(run.api_url, run.api_key, item, content_known)
        except Exception as exc:
            stats["errors"] += 1
            print(f"ERR[knowledge]: {item.key}: {exc}", file=sys.stderr)
            break
        written += 1
        stats["knowledge_embedded" if action == "embed" else "knowledge_metadata_updated"] += 1
        run.state[item.key] = {
            "revision": item.metadata["revision"],
            "metadata": _metadata_hash(item.metadata),
        }
        run.save(run.state)
    stats["knowledge_items_unindexed"] = 0 if run.dry_run else len(pending) - written
    if run.dry_run:
        return stats
    if not read_errors:
        keys = {item.key for item in items}
        for key in [k for k in run.state if k not in keys]:
            del run.state[key]
        run.save(run.state)
    _prune(run, items, read_errors, stats)
    if run.report_path is not None:
        _report(run.report_path, items, stats)
    return stats


def _prune(run: IndexRun, items: list[KnowledgeItem], read_errors: list[str], stats: dict) -> None:
    if not run.prune:
        return
    if read_errors:
        print(f"PRUNE[knowledge]: skipped — {len(read_errors)} source files unreadable")
        return
    if not items:
        print("PRUNE[knowledge]: skipped — no knowledge items scanned")
        return
    try:
        result = _post(
            run.api_url,
            run.api_key,
            "/prune",
            {"producer": PRODUCER, "keep_keys": sorted(i.key for i in items)},
        )
    except Exception as exc:
        stats["errors"] += 1
        print(f"WARN: knowledge prune failed: {exc}", file=sys.stderr)
        return
    stats["knowledge_pruned"] = result.get("deleted", 0)
    print(f"PRUNE[knowledge]: deleted={result.get('deleted', 0)} kept={result.get('kept', 0)}")


def _report(path: Path, items: list[KnowledgeItem], stats: dict) -> None:
    report = {
        "knowledge_scanned": stats["knowledge_scanned"],
        "knowledge_items_unindexed": stats["knowledge_items_unindexed"],
        "knowledge_read_errors": stats["knowledge_read_errors"],
        "unattributed": [
            {
                "key": i.key,
                "kind": i.kind,
                "path": i.metadata["source"]["path"],
                "entry_ts": i.metadata["source"]["entry_ts"],
            }
            for i in items
            if i.metadata["project_id"] == UNKNOWN
        ],
    }
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)
