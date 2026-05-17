#!/usr/bin/env python3
"""Mark active @signal blocks as resolved + prefix content with CLEARED.

Companion to dedupe_edges.py. Used after a root cause is fixed to retire
the active signals that were emitted while the bug was alive — without
waiting for the AI tick to issue CLEAR commands.

The brain_keeper tick treats `severity=resolved` + content starting with
`(CLEARED:` as a tombstone and removes the block from the active signal
list on the next pass.

Selectors are AND-combined. Provide at least one of --source / --severity
to avoid clobbering unrelated signals.

Usage:
    python3 tombstone_signals.py --vault /vault \
        --source brain-cron \
        --severity critical \
        --reason "edge dedup landed (commit 29f7d83); 422 dups + 66 self-loops removed"
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import markers  # noqa: E402

REGION_DIRS = (
    "clusters",
    "bridge",
    "left",
    "right",
    "frontal-lobe",
    "pineal",
    "amygdala",
)

SIGNAL_BLOCK_RE = re.compile(
    r"(<!--\s*@signal\b([^>]*?)-->)(.*?)(<!--\s*@/signal\s*-->)",
    re.DOTALL,
)


def _matches(attr_str: str, source: str | None, severity: str | None) -> bool:
    attrs = markers._parse_attrs(attr_str)
    if source is not None and attrs.get("source") != source:
        return False
    if severity is not None and attrs.get("severity") != severity:
        return False
    return True


def _process_file(
    path: Path,
    source: str | None,
    severity: str | None,
    reason: str,
    dry_run: bool,
) -> int:
    text = path.read_text()
    tombstoned = 0

    def rewrite(m: re.Match) -> str:
        nonlocal tombstoned
        open_tag, attr_str, body, close_tag = m.group(1), m.group(2), m.group(3), m.group(4)
        if not _matches(attr_str, source, severity):
            return m.group(0)
        if body.lstrip().startswith("(CLEARED:"):
            return m.group(0)  # already tombstoned
        attrs = markers._parse_attrs(attr_str)
        attrs["severity"] = "resolved"
        # Preserve key order best-effort by rebuilding from existing keys
        rebuilt = " ".join(f"{k}={v}" for k, v in attrs.items())
        new_open = f"<!-- @signal {rebuilt} -->"
        new_body = f"\n(CLEARED: {reason})\n{body.lstrip()}"
        tombstoned += 1
        return f"{new_open}{new_body}{close_tag}"

    new_text = SIGNAL_BLOCK_RE.sub(rewrite, text)

    if tombstoned and not dry_run:
        path.write_text(new_text)

    return tombstoned


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--vault", required=True)
    ap.add_argument("--source", default=None, help="Match @signal source= attr")
    ap.add_argument("--severity", default=None, help="Match @signal severity= attr")
    ap.add_argument("--reason", required=True, help="Short prefix appended to body")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if args.source is None and args.severity is None:
        print("ERROR: provide at least --source or --severity", file=sys.stderr)
        return 2

    vault = Path(args.vault).resolve()
    if not vault.is_dir():
        print(f"ERROR: vault {vault} not a directory", file=sys.stderr)
        return 1

    total = 0
    files_touched = 0
    for region in REGION_DIRS:
        region_dir = vault / region
        if not region_dir.is_dir():
            continue
        for md in region_dir.rglob("*.md"):
            n = _process_file(md, args.source, args.severity, args.reason, args.dry_run)
            if n:
                files_touched += 1
                print(f"  {md.relative_to(vault)}: tombstoned={n}")
                total += n

    prefix = "[dry-run] " if args.dry_run else ""
    print(f"\n{prefix}files_touched={files_touched} signals_tombstoned={total}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
