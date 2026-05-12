#!/usr/bin/env python3
"""Embed vault arc files into pgvector via the embeddings service.

For each arc markdown file under vault/clusters/**, build an embeddable text
blob (title + lessons + timeline) and POST it to the embeddings service /embed.
The target table is content_embeddings with producer='brain-arc'.

Runs diff-only: only re-embeds arcs whose mtime changed since the last run
(tracked in <vault>/.brain-arc-embed.state.json).

Usage:
    python3 embed_arcs.py --vault /vault [--dry-run] [--force-all]

Env:
    EMBEDDINGS_URL  default http://embeddings:8080  (alias: EMBED_API_URL)
    EMBEDDINGS_API_KEY  required unless --dry-run  (alias: EMBED_API_KEY)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path


STATE_FILENAME = ".brain-arc-embed.state.json"
MAX_TEXT_CHARS = 2000
REQ_TIMEOUT = 30


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Very simple frontmatter parser (no yaml dep)."""
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.DOTALL)
    if not m:
        return {}, text
    fm_raw, body = m.group(1), m.group(2)
    fm: dict = {}
    for line in fm_raw.splitlines():
        if ":" not in line or line.lstrip().startswith("-"):
            continue
        k, _, v = line.partition(":")
        fm[k.strip()] = v.strip().strip('"').strip("'")
    return fm, body


def extract_section(body: str, heading: str) -> str:
    """Return the text under '## <heading>' up to the next ## heading."""
    pat = re.compile(
        rf"^##\s+{re.escape(heading)}\s*$(.*?)(?=^##\s|\Z)",
        re.MULTILINE | re.DOTALL,
    )
    m = pat.search(body)
    return m.group(1).strip() if m else ""


def build_embed_text(fm: dict, body: str) -> str:
    """Compose the embeddable blob for an arc."""
    parts = []
    title = fm.get("title") or fm.get("cluster_id") or "untitled"
    parts.append(f"Title: {title}")
    region = fm.get("region", "")
    if region:
        parts.append(f"Region: {region}")
    lessons = extract_section(body, "Lessons")
    if lessons:
        parts.append(f"Lessons:\n{lessons}")
    timeline = extract_section(body, "Timeline")
    if timeline:
        parts.append(f"Timeline:\n{timeline}")
    resolution = extract_section(body, "Resolution")
    if resolution:
        parts.append(f"Resolution:\n{resolution}")
    text = "\n\n".join(parts)
    return text[:MAX_TEXT_CHARS]


def load_state(state_path: Path) -> dict[str, float]:
    if not state_path.exists():
        return {}
    try:
        return json.loads(state_path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def save_state(state_path: Path, state: dict[str, float]) -> None:
    state_path.write_text(json.dumps(state, indent=2, sort_keys=True))


def post_embed(api_url: str, api_key: str, payload: dict, timeout: int = REQ_TIMEOUT) -> dict:
    req = urllib.request.Request(
        url=f"{api_url.rstrip('/')}/embed",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


REGION_DIRS = ("bridge", "left", "right", "frontal-lobe", "pineal", "amygdala")


def _scan_dir(d: Path, recurse: bool = False):
    """Yield .md files from a directory, preferring .merged.md over raw .md."""
    if not d.is_dir():
        return
    pattern = "**/*.md" if recurse else "*.md"
    files = list(sorted(d.glob(pattern)))
    merged_stems = {
        f.name.replace(".merged.md", "") for f in files if f.name.endswith(".merged.md")
    }
    for md in files:
        if md.name.startswith("_"):
            continue
        stem = md.name[:-3]
        if not md.name.endswith(".merged.md") and stem in merged_stems:
            continue
        yield md


def scan_arcs(vault: Path):
    seen_ids: set[str] = set()

    # Region directories first (promoted/processed arcs — authoritative)
    for region in REGION_DIRS:
        for md in _scan_dir(vault / region, recurse=True):
            seen_ids.add(md.stem.replace(".merged", ""))
            yield md

    # Clusters directory (date-bucketed raw arcs — only if not already seen in a region)
    clusters_dir = vault / "clusters"
    if clusters_dir.is_dir():
        for date_dir in sorted(clusters_dir.iterdir()):
            if not date_dir.is_dir():
                continue
            for md in _scan_dir(date_dir):
                arc_id = md.stem.replace(".merged", "")
                if arc_id not in seen_ids:
                    seen_ids.add(arc_id)
                    yield md


def main() -> int:
    ap = argparse.ArgumentParser(description="Embed brain arcs into pgvector")
    ap.add_argument("--vault", required=True, help="vault root path")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force-all", action="store_true",
                    help="re-embed every arc, ignoring state")
    ap.add_argument("--api-url", default=(
        os.environ.get("EMBEDDINGS_URL")
        or os.environ.get("EMBED_API_URL", "http://embeddings:8080")
    ))
    ap.add_argument("--api-key", default=(
        os.environ.get("EMBEDDINGS_API_KEY")
        or os.environ.get("EMBED_API_KEY", "")
    ))
    ap.add_argument("--prune", action="store_true",
                    help="after embedding, POST /prune with the cluster_id "
                         "set of every arc seen — deletes orphan rows for "
                         "arcs that were graduated/renamed/deleted")
    args = ap.parse_args()

    vault = Path(args.vault)
    if not vault.is_dir():
        print(f"ERROR: vault dir not found: {vault}", file=sys.stderr)
        return 1

    state_path = vault / STATE_FILENAME
    if not os.access(str(vault), os.W_OK):
        state_path = Path("/tmp") / STATE_FILENAME
    state = {} if args.force_all else load_state(state_path)

    if not args.dry_run and not args.api_key:
        print("ERROR: EMBED_API_KEY not set (use --dry-run for a preview)",
              file=sys.stderr)
        return 1

    stats = {"scanned": 0, "embedded": 0, "skipped_unchanged": 0,
             "skipped_noop": 0, "errors": 0}
    # Cluster IDs of every arc we saw on disk — used for the prune call so
    # that pgvector rows whose source file has disappeared get deleted.
    seen_keys: set[str] = set()

    t0 = time.time()
    for md in scan_arcs(vault):
        stats["scanned"] += 1
        rel = str(md.relative_to(vault))
        mtime = md.stat().st_mtime
        if state.get(rel) == mtime:
            # Still record the cluster_id so prune doesn't delete unchanged arcs.
            try:
                fm_only, _ = parse_frontmatter(md.read_text(encoding="utf-8"))
                seen_keys.add(fm_only.get("cluster_id") or md.stem)
            except OSError:
                pass
            stats["skipped_unchanged"] += 1
            continue

        try:
            text = md.read_text(encoding="utf-8")
            fm, body = parse_frontmatter(text)
            content = build_embed_text(fm, body)
        except OSError as e:
            print(f"WARN: cannot read {md}: {e}", file=sys.stderr)
            stats["errors"] += 1
            continue

        if len(content) < 50:
            seen_keys.add(fm.get("cluster_id") or md.stem)
            stats["skipped_noop"] += 1
            continue

        cluster_id = fm.get("cluster_id") or md.stem
        seen_keys.add(cluster_id)
        payload = {
            "key": cluster_id,
            "content": content,
            "producer": "brain-arc",
            "content_type": "arc",
            "metadata": {
                "region": fm.get("region", ""),
                "heat": fm.get("heat", "0"),
                "status": fm.get("status", ""),
                "title": fm.get("title", ""),
                "path": rel,
            },
        }

        if args.dry_run:
            print(f"DRY: {cluster_id}  heat={payload['metadata']['heat']}  "
                  f"chars={len(content)}")
            stats["embedded"] += 1
            state[rel] = mtime
            continue

        try:
            resp = post_embed(args.api_url, args.api_key, payload)
            stats["embedded"] += 1
            state[rel] = mtime
            print(f"OK: {cluster_id} chunks={resp.get('chunks_stored')}")
        except Exception as e:
            stats["errors"] += 1
            print(f"ERR: {cluster_id}: {e}", file=sys.stderr)

    if not args.dry_run:
        save_state(state_path, state)

    # Reaper: delete pgvector rows whose cluster_id no longer maps to an
    # arc file on disk (graduated, renamed, or removed).
    if args.prune and not args.dry_run:
        try:
            prune_payload = {
                "producer": "brain-arc",
                "keep_keys": sorted(seen_keys),
            }
            req = urllib.request.Request(
                f"{args.api_url.rstrip('/')}/prune",
                data=json.dumps(prune_payload).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {args.api_key}",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=REQ_TIMEOUT) as resp:
                pr = json.loads(resp.read())
            stats["pruned"] = pr.get("deleted", 0)
            stats["pruned_kept"] = pr.get("kept", 0)
            print(f"PRUNE: deleted={stats['pruned']} kept={stats['pruned_kept']}")
        except Exception as e:
            stats["prune_error"] = str(e)
            print(f"WARN: prune failed: {e}", file=sys.stderr)

    stats["elapsed_sec"] = round(time.time() - t0, 3)
    json.dump(stats, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0 if stats["errors"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
