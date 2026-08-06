#!/usr/bin/env python3
"""Embed vault raw/ ingest notes into pgvector via the embeddings service.

raw/ is the ingest staging area — /ingest and /vault/write_inbox land text
there, but until this script nothing indexed it, so kb_search could never see
ingested content. Each raw/**/*.md file embeds as one document with
producer='brain-raw' (arcs keep their own 'brain-arc' producer and are
untouched by this script's prune).

Runs diff-only via <vault>/.brain-raw-embed.state.json, same mechanics as
embed_arcs.py.

Usage:
    python3 embed_raw.py --vault /vault [--dry-run] [--force-all] [--prune]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

from embed_arcs import REQ_TIMEOUT, load_state, parse_frontmatter, post_embed, save_state

STATE_FILENAME = ".brain-raw-embed.state.json"
MAX_TEXT_CHARS = 4000
PRODUCER = "brain-raw"


def scan_raw(vault: Path):
    raw_dir = vault / "raw"
    if not raw_dir.is_dir():
        return
    for md in sorted(raw_dir.rglob("*.md")):
        if md.name.startswith(("_", ".")):
            continue
        yield md


def build_embed_text(fm: dict, body: str, rel: str) -> str:
    parts = []
    title = fm.get("title") or Path(rel).stem
    parts.append(f"Title: {title}")
    if fm.get("source"):
        parts.append(f"Source: {fm['source']}")
    text = body.strip()
    if text:
        parts.append(text)
    return "\n\n".join(parts)[:MAX_TEXT_CHARS]


def main() -> int:
    ap = argparse.ArgumentParser(description="Embed vault raw/ notes into pgvector")
    ap.add_argument("--vault", required=True, help="vault root path")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force-all", action="store_true", help="re-embed every note, ignoring state")
    ap.add_argument(
        "--api-url",
        default=(
            os.environ.get("EMBEDDINGS_URL")
            or os.environ.get("EMBED_API_URL", "http://embeddings:8080")
        ),
    )
    ap.add_argument(
        "--api-key",
        default=(os.environ.get("EMBEDDINGS_API_KEY") or os.environ.get("EMBED_API_KEY", "")),
    )
    ap.add_argument(
        "--prune",
        action="store_true",
        help="delete pgvector rows for raw notes no longer on disk",
    )
    args = ap.parse_args()

    vault = Path(args.vault)
    if not vault.is_dir():
        print(f"ERROR: vault dir not found: {vault}", file=sys.stderr)
        return 1

    state_path = vault / STATE_FILENAME
    if not os.access(str(vault), os.W_OK):
        state_path = Path("/tmp") / STATE_FILENAME
    state = {} if args.force_all else load_state(state_path)

    if not args.api_key and not args.dry_run:
        print("ERROR: EMBED_API_KEY not set (use --dry-run for a preview)", file=sys.stderr)
        return 1

    stats = {"scanned": 0, "embedded": 0, "skipped_unchanged": 0, "skipped_noop": 0, "errors": 0}
    seen_keys: set[str] = set()

    t0 = time.time()
    for md in scan_raw(vault):
        stats["scanned"] += 1
        rel = str(md.relative_to(vault))
        key = rel.replace("/", ":")
        mtime = md.stat().st_mtime
        if state.get(rel) == mtime:
            seen_keys.add(key)
            stats["skipped_unchanged"] += 1
            continue

        try:
            fm, body = parse_frontmatter(md.read_text(encoding="utf-8"))
            content = build_embed_text(fm, body, rel)
        except OSError as e:
            print(f"WARN: cannot read {md}: {e}", file=sys.stderr)
            stats["errors"] += 1
            continue

        if len(content) < 50:
            seen_keys.add(key)
            stats["skipped_noop"] += 1
            continue

        seen_keys.add(key)
        payload = {
            "key": key,
            "content": content,
            "producer": PRODUCER,
            "content_type": "raw-note",
            "metadata": {"path": rel, "title": fm.get("title", ""), "source": fm.get("source", "")},
        }

        if args.dry_run:
            print(f"DRY: {key} chars={len(content)}")
            stats["embedded"] += 1
            state[rel] = mtime
            continue

        try:
            resp = post_embed(args.api_url, args.api_key, payload)
            stats["embedded"] += 1
            state[rel] = mtime
            print(f"OK: {key} chunks={resp.get('chunks_stored')}")
        except Exception as e:
            stats["errors"] += 1
            print(f"ERR: {key}: {e}", file=sys.stderr)

    if not args.dry_run:
        save_state(state_path, state)

    # Prune only when raw/ is verifiably present. An absent directory (fresh
    # vault, transient NFS/bind-mount hiccup) yields an empty keep_keys, and
    # POSTing that would delete EVERY brain-raw row server-side. An existing
    # but empty raw/ is a legitimate "operator deleted everything" state and
    # prunes normally.
    if args.prune and not args.dry_run and not (vault / "raw").is_dir():
        print("PRUNE: skipped — raw/ absent (fresh vault or transient mount)")
    elif args.prune and not args.dry_run:
        try:
            req = urllib.request.Request(
                f"{args.api_url.rstrip('/')}/prune",
                data=json.dumps({"producer": PRODUCER, "keep_keys": sorted(seen_keys)}).encode(
                    "utf-8"
                ),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {args.api_key}",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=REQ_TIMEOUT) as resp:
                pr = json.loads(resp.read())
            stats["pruned"] = pr.get("deleted", 0)
            print(f"PRUNE: deleted={stats['pruned']} kept={pr.get('kept', 0)}")
        except Exception as e:
            stats["prune_error"] = str(e)
            print(f"WARN: prune failed: {e}", file=sys.stderr)

    stats["elapsed_sec"] = round(time.time() - t0, 3)
    json.dump(stats, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0 if stats["errors"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
