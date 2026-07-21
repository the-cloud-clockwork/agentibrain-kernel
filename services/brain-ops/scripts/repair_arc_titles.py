#!/usr/bin/env python3
"""One-time repair — strip merge rationale out of arc `title:` frontmatter.

`brain_apply.parse_merges` captured a MERGE line's title with a pattern
anchored at end-of-line, so it swallowed the justification the model wrote
after the new arc name:

    MERGE: a + b → `unified-id` — identical session ID, continuous work…

`apply_merges` then wrote that whole string into the merged arc's `title:`.
The result is an arc whose display name — in `/feed`, in hot-arc tables, in
every search result — is a paragraph of reasoning rather than a name.

The producing bug is fixed upstream, so no NEW arcs are affected. This repairs
the ones already written.

Scope, stated honestly: the kernel's own `markers.parse_frontmatter` is a line
splitter, not a YAML parser, and loads these titles verbatim. No arc is
invisible because of this and nothing is being rescued from unreadability. The
damage is legibility, and that is the whole of it.

What counts as polluted is deliberately conservative — a legitimate title may
well contain an em-dash (`Session markers — de4d189d`, `Brain smoke tests —
2026-04-13`) and those must survive untouched. See `is_polluted`.

Usage:
    python3 scripts/repair_arc_titles.py --vault /vault --dry-run
    python3 scripts/repair_arc_titles.py --vault /vault
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tarfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from brain_apply import clean_merge_title  # noqa: E402

# An em-dash alone means nothing — plenty of good titles use one as a subtitle
# separator ("KB Pipeline — federated search + synthesis + dispatch"). Only a
# long clause that is also punctuated like a sentence reads as rationale rather
# than as a subtitle.
_MIN_RATIONALE_WORDS = 12

_TITLE_RE = re.compile(r'^title:[ \t]*(.*)$', re.MULTILINE)
_SEPARATOR_RE = re.compile(r'\s(?:[—–]|--)\s')


def is_polluted(title: str) -> bool:
    """True when a title carries merge justification after the arc name.

    Two tells, and the second is deliberately hard to trip:

    * a backtick — the model quotes the new id as ```new-id``` and its closing
      backtick lands mid-title. A hand-written title has no reason to hold one,
      so this alone is conclusive.
    * a separator followed by a clause that is both long AND internally
      punctuated. Length alone is not enough: ``— federated search + synthesis
      + dispatch`` is a subtitle and must survive, while ``— both are
      right-hemisphere complete arcs; the separate arc is redundant`` is an
      argument. The comma or semicolon is what marks the difference.

    False negatives here are cheap — one ugly title survives. False positives
    destroy a real name, so the bar sits high on purpose.
    """
    if not title:
        return False
    if '`' in title:
        return True
    parts = _SEPARATOR_RE.split(title, maxsplit=1)
    if len(parts) != 2:
        return False
    clause = parts[1]
    return len(clause.split()) >= _MIN_RATIONALE_WORDS and re.search(r'[,;]', clause) is not None


def _backup(vault: Path, backup_dir: Path) -> Path:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    backup_dir.mkdir(parents=True, exist_ok=True)
    archive = backup_dir / f"vault-pre-title-repair-{ts}.tgz"
    backup_resolved = backup_dir.resolve()

    def _filter(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
        target = (vault / info.name).resolve()
        if target == backup_resolved or str(target).startswith(str(backup_resolved) + "/"):
            return None
        return info

    with tarfile.open(archive, "w:gz") as tar:
        tar.add(vault, arcname=".", filter=_filter)
    return archive


def repair(vault: Path, dry_run: bool, limit: int | None = None) -> dict:
    stats = {"scanned": 0, "polluted": 0, "repaired": 0, "unchanged": 0, "emptied": 0}
    samples: list[dict] = []

    for path in sorted(vault.rglob("*.md")):
        if "_backups" in path.parts:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if not text.startswith("---"):
            continue

        head, sep, rest = text.partition("---")[2].partition("---")
        if not sep:
            continue

        m = _TITLE_RE.search(head)
        if not m:
            continue
        stats["scanned"] += 1
        original = m.group(1).strip()
        if not is_polluted(original):
            continue
        stats["polluted"] += 1

        cleaned = clean_merge_title(original)
        if not cleaned:
            # Never blank a title — a nameless arc is worse than an ugly one.
            stats["emptied"] += 1
            continue
        if cleaned == original:
            stats["unchanged"] += 1
            continue

        if len(samples) < 15:
            samples.append({"path": str(path.relative_to(vault)),
                            "before": original[:110], "after": cleaned})

        if not dry_run:
            new_head = _TITLE_RE.sub(f"title: {json.dumps(cleaned)}", head, count=1)
            path.write_text("---" + new_head + "---" + rest, encoding="utf-8")
        stats["repaired"] += 1

        if limit and stats["repaired"] >= limit:
            break

    stats["samples"] = samples
    return stats


def main() -> int:
    ap = argparse.ArgumentParser(description="Repair merge-rationale pollution in arc titles")
    ap.add_argument("--vault", required=True)
    ap.add_argument("--dry-run", action="store_true", help="Report only; no writes")
    ap.add_argument("--limit", type=int, default=None, help="Stop after N repairs")
    ap.add_argument("--backup-dir", default=None)
    ap.add_argument("--no-backup", action="store_true")
    args = ap.parse_args()

    vault = Path(args.vault).resolve()
    if not vault.is_dir():
        print(f"ERROR: vault {vault} is not a directory", file=sys.stderr)
        return 1

    result: dict = {"dry_run": args.dry_run, "backup": None}
    if not args.dry_run and not args.no_backup:
        backup_dir = Path(args.backup_dir) if args.backup_dir else vault / "_backups"
        archive = _backup(vault, backup_dir)
        result["backup"] = str(archive)
        print(f"BACKUP: {archive} ({archive.stat().st_size} bytes)", file=sys.stderr)

    result["titles"] = repair(vault, args.dry_run, args.limit)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
