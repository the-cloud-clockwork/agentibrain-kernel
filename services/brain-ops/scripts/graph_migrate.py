#!/usr/bin/env python3
"""Migrate vault edge markers into a normalized graph projection (SV2-GRF-02).

Reads every arc's frontmatter ``edges:`` and inline ``@edge`` markers, writes
relations.json, quarantine.json and audit.json plus a manifest of input and
output hashes into --out. The vault is never written. Operator review
decisions live in reviews.json in --out, keyed by quarantine id; the migrator
reads it and never writes it. A rerun over the same inputs is byte identical.
Before a changed projection replaces an intact one, the intact one is kept in
previous/, and --rollback restores it after checking its manifest.

Usage:
    python3 graph_migrate.py --vault /vault --out /vault-state/graph-migration
    python3 graph_migrate.py --vault /vault --out DIR --dry-run
    python3 graph_migrate.py --out DIR --rollback
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
brain_keeper = importlib.import_module("brain_keeper")
edge_relations = importlib.import_module("edge_relations")

PROJECTION = ("relations.json", "quarantine.json", "audit.json")
MANIFEST = "manifest.json"
REVIEWS = "reviews.json"
PREVIOUS = "previous"


class RollbackRefused(RuntimeError):
    pass


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _dump(value) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()


def _atomic_write(path: Path, data: bytes) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def _arc_files(vault: Path) -> list[Path]:
    files = set()
    for region in ("clusters", *brain_keeper.REGION_DIRS):
        root = vault / region
        if root.is_dir():
            files.update(root.rglob("*.md"))
    return sorted(files, key=lambda p: p.relative_to(vault).as_posix())


def scan(vault: Path) -> tuple[list, dict[str, str], dict[str, str]]:
    assertions, inputs, projects = [], {}, {}
    for path in _arc_files(vault):
        rel = path.relative_to(vault).as_posix()
        data = path.read_bytes()
        inputs[rel] = _sha(data)
        text = data.decode()
        projects[edge_relations.arc_id(text, path)] = edge_relations.arc_project(text)
        assertions.extend(edge_relations.file_assertions(path, rel))
    return assertions, inputs, projects


def _reviews(out: Path) -> tuple[dict, str | None]:
    path = out / REVIEWS
    if not path.is_file():
        return {}, None
    data = path.read_bytes()
    reviews = json.loads(data)
    if not isinstance(reviews, dict):
        raise ValueError(f"{path} must hold an object keyed by quarantine id")
    return reviews, _sha(data)


def _intact(directory: Path) -> dict | None:
    path = directory / MANIFEST
    if not path.is_file():
        return None
    manifest = json.loads(path.read_text())
    for name, digest in manifest.get("outputs", {}).items():
        target = directory / name
        if not target.is_file() or _sha(target.read_bytes()) != digest:
            return None
    return manifest


def _keep_previous(out: Path) -> None:
    previous = out / PREVIOUS
    previous.mkdir(exist_ok=True)
    for name in (*PROJECTION, MANIFEST):
        _atomic_write(previous / name, (out / name).read_bytes())


def migrate(vault: Path, out: Path, dry_run: bool = False) -> dict:
    reviews, reviews_sha = _reviews(out)
    assertions, inputs, projects = scan(vault)
    projection = edge_relations.normalize(assertions, reviews, projects)
    docs = {
        "relations.json": _dump(projection["relations"]),
        "quarantine.json": _dump(projection["quarantine"]),
        "audit.json": _dump(projection["audit"]),
    }
    quarantined = sum(1 for d in projection["audit"] if d["decision"] == "quarantined")
    manifest = {
        "package": "SV2-GRF-02",
        "inputs": inputs,
        "reviews": reviews_sha,
        "outputs": {name: _sha(data) for name, data in docs.items()},
        "relations": len(projection["relations"]),
        "graph_migration_quarantined_edges": quarantined,
    }
    if dry_run:
        return manifest
    out.mkdir(parents=True, exist_ok=True)
    current = _intact(out)
    if current is not None and current["outputs"] != manifest["outputs"]:
        _keep_previous(out)
    for name, data in docs.items():
        _atomic_write(out / name, data)
    _atomic_write(out / MANIFEST, _dump(manifest))
    return manifest


def rollback(out: Path) -> dict:
    previous = out / PREVIOUS
    manifest = _intact(previous)
    if manifest is None:
        raise RollbackRefused(f"{previous} holds no projection that matches its manifest")
    for name in PROJECTION:
        _atomic_write(out / name, (previous / name).read_bytes())
    _atomic_write(out / MANIFEST, (previous / MANIFEST).read_bytes())
    return manifest


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--vault", help="Vault root path")
    ap.add_argument("--out", required=True, help="Projection directory")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--rollback", action="store_true")
    args = ap.parse_args(argv)
    out = Path(args.out).resolve()

    if args.rollback:
        try:
            manifest = rollback(out)
        except RollbackRefused as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        print(f"restored relations={manifest['relations']}")
        return 0

    if not args.vault or not Path(args.vault).is_dir():
        print(f"ERROR: vault {args.vault} not a directory", file=sys.stderr)
        return 1
    manifest = migrate(Path(args.vault).resolve(), out, args.dry_run)
    prefix = "[dry-run] " if args.dry_run else ""
    print(
        f"{prefix}arcs={len(manifest['inputs'])} relations={manifest['relations']} "
        f"graph_migration_quarantined_edges={manifest['graph_migration_quarantined_edges']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
