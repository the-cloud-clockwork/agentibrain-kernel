#!/usr/bin/env python3
"""One-time vault repair: collapse runaway merge chains + dedupe edges.

Companion to the brain_apply.py merge/edge fixes. Run ONCE against a vault that
accumulated the ``X.merged.merged…md`` filename runaway (the merge step renamed
already-merged tombstones, stacking ``.merged`` suffixes and re-appending each
arc's body + embedded @edge markers every tick — 6,887 edges, prompts over the
model context window, tick health 0/nuclear).

Phases:
  0. Backup — tar the whole vault to ``--backup-dir`` (skipped on --dry-run /
     --no-backup). The cleanup deletes files, so the snapshot is the rollback.
  1. Chain-collapse — within each directory, group files by canonical arc id
     (``markers.canonical_arc_id`` folds the whole .merged chain to one id).
     For every group that is a chain (>1 file, or a single ``X.merged.merged…``
     of depth >= 2), keep the LONGEST body as the survivor, dedup its repeated
     ``## Merged from <X>`` sections, delete the rest, and write the survivor
     back under the canonical ``<id>.md`` name.
  2. Edge dedup — run the existing ``dedupe_edges._process_file`` over every
     surviving file (one canonical edge per target, drop self-loops, collapse
     multi-type collisions).

Idempotent — a second run is a no-op. Reuses ``markers``, ``brain_apply._rank``
(via dedupe_edges) and ``dedupe_edges._process_file``; no logic is duplicated.

Usage:
    python3 scripts/vault_cleanup.py --vault /vault --dry-run
    python3 scripts/vault_cleanup.py --vault /vault
    python3 scripts/vault_cleanup.py --vault /vault --no-backup
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import tarfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

# This script lives one level below the modules; add the parent for markers.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import dedupe_edges  # noqa: E402  # reuse REGION_DIRS + _process_file
import markers  # noqa: E402

# Split a body on "## Merged from <id>" headers, capturing the id.
_MERGE_NOTE_RE = re.compile(r'(?m)^## Merged from (\S+)\s*$')


def _merged_depth(name: str) -> int:
    """Count trailing ``.merged`` segments (``X.merged.merged.md`` -> 2)."""
    stem = name[:-3] if name.endswith(".md") else name
    depth = 0
    while stem.endswith(".merged"):
        stem = stem[: -len(".merged")]
        depth += 1
    return depth


def _split_header_body(text: str) -> tuple[str, str]:
    """Return (frontmatter_header_verbatim, body). Header is '' when absent."""
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) == 3:
            return f"---{parts[1]}---", parts[2]
    return "", text


def _dedup_merge_notes(text: str) -> tuple[str, int]:
    """Drop duplicate ``## Merged from <X>`` sections, keeping the first per X.

    Returns (new_text, sections_dropped). The runaway re-merged the same arc
    every tick, so a survivor can carry dozens of identical merge notes.
    """
    header, body = _split_header_body(text)
    pieces = _MERGE_NOTE_RE.split(body)
    if len(pieces) == 1:
        return text, 0  # no merge notes
    base = pieces[0].rstrip()
    out = [base] if base else []
    seen: set[str] = set()
    dropped = 0
    rest = pieces[1:]
    for i in range(0, len(rest) - 1, 2):
        name, section = rest[i], rest[i + 1]
        if name in seen:
            dropped += 1
            continue
        seen.add(name)
        out.append(f"## Merged from {name}\n{section.rstrip()}")
    new_body = "\n\n".join(p for p in out if p.strip()) + "\n"
    new_text = f"{header}\n{new_body}" if header else new_body
    return new_text, dropped


def _backup(vault: Path, backup_dir: Path) -> Path:
    """Tar the vault (excluding the backup dir) and return the archive path."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    backup_dir.mkdir(parents=True, exist_ok=True)
    archive = backup_dir / f"vault-pre-cleanup-{ts}.tgz"
    backup_resolved = backup_dir.resolve()

    def _filter(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
        # Never recurse into the backup dir itself.
        if backup_resolved == (vault / info.name).resolve() or str(
            (vault / info.name).resolve()
        ).startswith(str(backup_resolved) + "/"):
            return None
        return info

    with tarfile.open(archive, "w:gz") as tar:
        tar.add(vault, arcname=".", filter=_filter)
    return archive


def collapse_chains(vault: Path, dry_run: bool) -> dict:
    """Phase 1 — collapse merge-chain duplicate files per directory."""
    stats = {"groups_scanned": 0, "chains_collapsed": 0,
             "files_deleted": 0, "merge_notes_deduped": 0}

    for region in dedupe_edges.REGION_DIRS:
        region_dir = vault / region
        if not region_dir.is_dir():
            continue
        # Group by (directory, canonical id) — mirrors the dir-bucketed
        # suppression in brain_apply / brain_keeper.
        groups: dict[tuple[Path, str], list[Path]] = defaultdict(list)
        for md in region_dir.rglob("*.md"):
            if md.name.startswith("_"):
                continue
            groups[(md.parent, markers.canonical_arc_id(md.name))].append(md)

        for (parent, canonical), files in groups.items():
            stats["groups_scanned"] += 1
            is_chain = len(files) > 1 or any(_merged_depth(f.name) >= 2 for f in files)
            if not is_chain:
                continue

            # Survivor = longest body (it accumulated the most merge history).
            survivor = max(files, key=lambda f: len(f.read_text()))
            new_text, dropped = _dedup_merge_notes(survivor.read_text())
            target = parent / f"{canonical}.md"

            print(
                f"  COLLAPSE {region}/…/{canonical}: {len(files)} files "
                f"-> {target.name} (merge_notes_dropped={dropped}, "
                f"survivor={survivor.name})"
            )
            stats["chains_collapsed"] += 1
            stats["files_deleted"] += len(files) - 1
            stats["merge_notes_deduped"] += dropped

            if not dry_run:
                for f in files:
                    f.unlink()
                target.write_text(new_text)

    return stats


def dedupe_all_edges(vault: Path, dry_run: bool) -> dict:
    """Phase 2 — per-file edge dedup over survivors (reuses dedupe_edges)."""
    stats = {"edges_kept": 0, "edges_removed": 0, "files_touched": 0}
    for region in dedupe_edges.REGION_DIRS:
        region_dir = vault / region
        if not region_dir.is_dir():
            continue
        for md in region_dir.rglob("*.md"):
            if md.name.startswith("_"):
                continue
            kept, dedup, selfloop, collapsed = dedupe_edges._process_file(md, dry_run)
            removed = dedup + selfloop + collapsed
            stats["edges_kept"] += kept
            stats["edges_removed"] += removed
            if removed:
                stats["files_touched"] += 1
    return stats


def main() -> int:
    ap = argparse.ArgumentParser(description="One-time vault merge-chain + edge cleanup")
    ap.add_argument("--vault", required=True, help="Vault root path")
    ap.add_argument("--dry-run", action="store_true", help="Report only; no writes/deletes")
    ap.add_argument("--backup-dir", default=None,
                    help="Backup tarball directory (default: <vault>/_backups)")
    ap.add_argument("--no-backup", action="store_true", help="Skip the pre-cleanup tar")
    args = ap.parse_args()

    vault = Path(args.vault).resolve()
    if not vault.is_dir():
        print(f"ERROR: vault {vault} not a directory", file=sys.stderr)
        return 1

    result: dict = {"dry_run": args.dry_run, "backup": None}

    if not args.dry_run and not args.no_backup:
        backup_dir = Path(args.backup_dir) if args.backup_dir else vault / "_backups"
        archive = _backup(vault, backup_dir)
        result["backup"] = str(archive)
        print(f"BACKUP: {archive} ({archive.stat().st_size} bytes)", file=sys.stderr)

    print("--- Phase 1: collapse merge chains ---", file=sys.stderr)
    result["collapse"] = collapse_chains(vault, args.dry_run)
    print("--- Phase 2: dedupe edges ---", file=sys.stderr)
    result["edges"] = dedupe_all_edges(vault, args.dry_run)

    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
