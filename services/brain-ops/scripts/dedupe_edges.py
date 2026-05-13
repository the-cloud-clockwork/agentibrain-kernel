#!/usr/bin/env python3
"""One-shot vault cleanup: dedupe @edge markers, remove self-loops, normalize IDs.

Run once after the brain_apply.py edge-dedup fix lands to clear ~3 weeks of
accumulated noise. Idempotent — a second run is a no-op.

Walks every .md file under the vault region dirs plus clusters/, finds all
inline `<!-- @edge type=X target=Y -->` markers, and:

  - Strips backticks from the `target` attribute (normalization)
  - Drops self-loops (target_norm == source_norm where source is the
    file's cluster_id or stem)
  - Collapses multi-type same-target collisions: when a file has
    `child` and `parent` both pointing at the same arc, keeps the
    highest-priority type (parent > child > sibling > unblocks >
    supersedes > related). Priority is imported from brain_apply._rank.
  - Dedupes within a file by target (one canonical edge per pair)

Usage:
    python3 dedupe_edges.py --vault /vault
    python3 dedupe_edges.py --vault /vault --dry-run
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# Find on path: brain_apply.py imports markers as sibling; this script lives
# one level deeper, so add the parent.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import markers  # noqa: E402
from brain_apply import _rank  # noqa: E402  # shared edge-type priority

REGION_DIRS = (
    "clusters",
    "bridge",
    "left",
    "right",
    "frontal-lobe",
    "pineal",
    "amygdala",
)

EDGE_RE = re.compile(r"<!--\s*@edge\s+([^>]*?)\s*-->")


def _normalize_id(s: str) -> str:
    return s.strip().strip("`")


def _arc_id_for(path: Path) -> str:
    """Best-effort source ID: frontmatter cluster_id, fall back to file stem."""
    try:
        fm, _ = markers.parse_frontmatter(path.read_text())
        cid = fm.get("cluster_id")
        if cid:
            return _normalize_id(str(cid))
    except Exception:
        pass
    return _normalize_id(path.stem)


def _process_file(path: Path, dry_run: bool) -> tuple[int, int, int, int]:
    """Return (kept, dedup_dropped, selfloop_dropped, type_collapsed)."""
    text = path.read_text()
    src_id = _arc_id_for(path)

    # Pass 1: pick the highest-priority type per target. Track how many
    # losing variants will be dropped so the run report distinguishes
    # plain duplicates from multi-type collisions.
    best_by_target: dict[str, str] = {}
    type_collapsed = 0
    seen_pairs: set[tuple[str, str]] = set()
    for match in EDGE_RE.finditer(text):
        attrs = markers._parse_attrs(match.group(1))
        etype = attrs.get("type", "")
        target = _normalize_id(attrs.get("target", ""))
        if not etype or not target:
            continue
        if target == src_id:
            continue  # accounted for in pass 2

        pair = (etype, target)
        if pair in seen_pairs:
            continue  # plain (type,target) dup; counted in pass 2
        seen_pairs.add(pair)

        cur = best_by_target.get(target)
        if cur is None:
            best_by_target[target] = etype
        elif _rank(etype) < _rank(cur):
            best_by_target[target] = etype
            type_collapsed += 1  # previous winner now loses
        else:
            type_collapsed += 1  # this one loses to the current winner

    # Pass 2: emit one canonical marker per target; drop self-loops and
    # any subsequent occurrence of the same target.
    kept = 0
    dedup_dropped = 0
    selfloop_dropped = 0
    emitted: set[str] = set()

    def replace(match: re.Match) -> str:
        nonlocal kept, dedup_dropped, selfloop_dropped
        attrs = markers._parse_attrs(match.group(1))
        etype = attrs.get("type", "")
        target = _normalize_id(attrs.get("target", ""))
        if not etype or not target:
            return match.group(0)  # malformed — leave alone

        if target == src_id:
            selfloop_dropped += 1
            return ""

        if target in emitted:
            dedup_dropped += 1
            return ""

        winning = best_by_target.get(target, etype)
        emitted.add(target)
        kept += 1
        return f"<!-- @edge type={winning} target={target} -->"

    new_text = EDGE_RE.sub(replace, text)

    # Collapse runs of blank lines left behind by removed markers.
    new_text = re.sub(r"\n{3,}", "\n\n", new_text)

    if new_text != text and not dry_run:
        path.write_text(new_text)

    return kept, dedup_dropped, selfloop_dropped, type_collapsed


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--vault", required=True, help="Vault root path")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    vault = Path(args.vault).resolve()
    if not vault.is_dir():
        print(f"ERROR: vault {vault} not a directory", file=sys.stderr)
        return 1

    total_kept = 0
    total_dedup = 0
    total_selfloop = 0
    total_type_collapsed = 0
    files_touched = 0

    for region in REGION_DIRS:
        region_dir = vault / region
        if not region_dir.is_dir():
            continue
        for md in region_dir.rglob("*.md"):
            before = md.read_text()
            kept, dedup, selfloop, type_collapsed = _process_file(md, args.dry_run)
            after = md.read_text() if not args.dry_run else before
            if dedup or selfloop or type_collapsed or after != before:
                files_touched += 1
                print(
                    f"  {md.relative_to(vault)}: kept={kept} "
                    f"dedup_dropped={dedup} selfloop_dropped={selfloop} "
                    f"type_collapsed={type_collapsed}"
                )
            total_kept += kept
            total_dedup += dedup
            total_selfloop += selfloop
            total_type_collapsed += type_collapsed

    prefix = "[dry-run] " if args.dry_run else ""
    print(
        f"\n{prefix}files_touched={files_touched} "
        f"edges_kept={total_kept} "
        f"dedup_dropped={total_dedup} "
        f"selfloop_dropped={total_selfloop} "
        f"type_collapsed={total_type_collapsed}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
