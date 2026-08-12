"""Self-healing reconcile for lesson-log files.

`brain_keeper.is_arc()` used to misread `lessons-YYYY-MM-DD.md` as a work arc,
because the filename embeds a date. The heat, graduation and promote/demote
machinery then moved those logs around the vault and cooled them to heat 0, so
the same day's lessons ended up split across `left/`, `left/reference/` and
`frontal-lobe/unconscious/` — in one case the same date living in two places at
once. `is_arc` no longer does that, but the scatter it already caused is still
on disk.

This module puts it back. Every `lessons-YYYY-MM-DD.md` found anywhere in the
vault is merged into the one canonical `left/reference/` copy for its date,
entries deduplicated by content hash, frontmatter backfilled, and the strays
removed. It runs on every tick rather than as a one-shot script, so a vault
heals itself the next time the stack comes up — including vaults on other
machines that nobody will remember to run a migration against.

Idempotence is the property that makes that safe: once a date has exactly one
log, at the canonical path, with frontmatter and no duplicate entries, the pass
does nothing at all — no write, no backup, no mtime change.

Nothing here deletes an entry. Two logs for one date merge to the union of
their entries; only byte-identical duplicates collapse, and every file removed
is copied into `_backups/lesson-reconcile/<stamp>/` first.
"""

from __future__ import annotations

import hashlib
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

import markers

# Canonical home for every lesson log. brain-api writes new ones here already;
# this module's job is making sure nothing ends up anywhere else.
CANONICAL_SUBDIR = ("left", "reference")

FM_DELIM = "---"

BACKUP_SUBDIR = ("_backups", "lesson-reconcile")

# `lessons-2026-08-11.md` -> `2026-08-11`
_DATE_FROM_NAME_RE = re.compile(r"^lessons-(\d{4}-\d{2}-\d{2})\.md$")

# Entry headers are matched via markers.LESSON_ENTRY_HEADER_RE — see the note
# there on why the timestamp is required rather than decorative.


def entry_hash(body: str) -> str:
    """Content hash of one lesson entry.

    Hashes the body only, never the header. The header carries a fresh
    timestamp and session id on every emission, so a header-inclusive hash
    would never match and would dedupe nothing — which is exactly how the same
    lesson came to sit in one file four times.

    Newlines are normalized first, matching `_content_hash` in brain-api's
    marker writer byte for byte — the two must agree, or a lesson deduped on
    write reappears here as a distinct entry.
    """
    normalized = body.replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(normalized.strip().encode("utf-8")).hexdigest()[:16]


def _preamble_and_body(text: str, pattern: re.Pattern) -> tuple[str, str]:
    """Split at the first entry header: (everything before, everything from).

    Deliberately does NOT parse frontmatter. `markers.parse_frontmatter` splits
    on the first two `---` in the file, so a legacy log whose first entry body
    contains a `---` — a horizontal rule, a diff marker, a quoted YAML sample —
    has that line read as the frontmatter close, and the entire entry above it,
    header included, is swallowed as "frontmatter" and dropped. Anchoring on the
    header instead means body content can never be mistaken for a delimiter.
    """
    m = pattern.search(text)
    if not m:
        return text, ""
    return text[: m.start()], text[m.start() :]


def split_entries(text: str) -> list[tuple[str, str]]:
    """Split a lesson log into (header_line, body) pairs.

    Everything before the first timestamped header is preamble — frontmatter or
    stray prose — and is discarded; the canonical frontmatter is regenerated on
    write.
    """
    _, body = _preamble_and_body(text, markers.LESSON_ENTRY_SPLIT_RE)
    entries: list[tuple[str, str]] = []
    # Split on a timestamped header only. A bare `## ` boundary would also fire
    # on a heading inside a lesson's own prose and tear one entry into two.
    for chunk in markers.LESSON_ENTRY_SPLIT_RE.split(body):
        lines = chunk.splitlines()
        if not lines or not markers.LESSON_ENTRY_HEADER_RE.match(lines[0].strip()):
            continue
        header = lines[0].strip()
        entry_body = "\n".join(lines[1:]).strip()
        if entry_body:
            entries.append((header, entry_body))
    return entries


_ANY_HEADER_RE = re.compile(r"(?m)^(?=##\s)")


def _permissive_blocks(text: str) -> list[str]:
    """Every `##` block body, regardless of header shape.

    Deliberately looser than `split_entries`, and deliberately sharing none of
    its parsing: a safety net that inherits the parser's blind spots cannot see
    what the parser destroys.
    """
    _, body = _preamble_and_body(text, _ANY_HEADER_RE)
    out = []
    for chunk in _ANY_HEADER_RE.split(body):
        lines = chunk.splitlines()
        if not lines or not lines[0].strip().startswith("##"):
            continue
        block_body = "\n".join(lines[1:]).strip()
        if block_body:
            out.append(block_body)
    return out


def _preamble_is_substantive(text: str) -> bool:
    """True when text before the first entry header is more than frontmatter.

    A file matching `lessons-YYYY-MM-DD.md` that carries real content but no
    timestamped entries is not a lesson log — it is something else wearing the
    name, such as a work arc with a `cluster_id` and a prose body. Rewriting it
    to bare lesson-log frontmatter would erase it, so it is refused instead.
    """
    preamble, _ = _preamble_and_body(text, markers.LESSON_ENTRY_SPLIT_RE)
    stripped = preamble.strip()
    if not stripped:
        return False
    # Drop a leading frontmatter block if one genuinely opens the file. Any
    # ambiguity here resolves toward "substantive", which refuses the merge —
    # the safe direction.
    if stripped.startswith(FM_DELIM):
        rest = stripped[len(FM_DELIM) :]
        close = rest.find(f"\n{FM_DELIM}")
        if close != -1:
            stripped = rest[close + len(FM_DELIM) + 1 :].strip()
    # A preamble that is only delimiter punctuation — an unterminated `---`, a
    # stray horizontal rule — carries no content worth refusing over.
    return bool(stripped.strip("-").strip())


def merge_is_lossless(new_text: str, sources: list[Path]) -> bool:
    """True when every block on disk survives into the merged text.

    This pass deletes files, so a parser assumption that turns out to be wrong
    would silently destroy lessons. Rather than trust the strict entry parser,
    verify the result: if any block the permissive reader can see is missing
    from the output, refuse the merge and leave the vault exactly as it is. A
    date that trips this is reported and skipped, never rewritten.
    """
    for path in sources:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return False
        for block in _permissive_blocks(text):
            if block not in new_text:
                return False
    return True


def entry_sort_key(header: str) -> str:
    """Sort key for an entry, taken from the timestamp in its header.

    Falls back to the raw header so an unparseable one keeps a stable position
    instead of throwing. ISO-8601 sorts correctly as a string.
    """
    m = markers.LESSON_ENTRY_HEADER_RE.match(header)
    return m.group(1) if m else header


def build_frontmatter(date_str: str) -> str:
    """Canonical frontmatter for a lesson log.

    Kept byte-identical to what brain-api's marker writer seeds on creation
    (`services/brain-api/app/markers.py`). The two live in different services
    and cannot share an import, so they agree by convention — if one changes,
    the other must, or this pass will rewrite every log on every tick and stop
    being a no-op.
    """
    return (
        "---\n"
        f"id: lessons-{date_str}\n"
        f"title: Lessons — {date_str}\n"
        "type: lesson-log\n"
        f"created: {date_str}\n"
        "---\n"
    )


def render_log(date_str: str, entries: list[tuple[str, str]]) -> str:
    """Render a complete lesson log. Deterministic: same entries, same bytes."""
    parts = [build_frontmatter(date_str)]
    for header, body in entries:
        parts.append(f"\n{header}\n\n{body}\n")
    return "".join(parts)


def find_logs(vault_root: Path) -> dict[str, list[Path]]:
    """Every lesson log in the vault, grouped by date, sorted by path."""
    by_date: dict[str, list[Path]] = {}
    for path in sorted(vault_root.rglob("lessons-*.md")):
        # The backup dir holds copies this very pass made. Walking back into it
        # would resurrect deleted strays on the next tick.
        if BACKUP_SUBDIR[0] in path.parts:
            continue
        m = _DATE_FROM_NAME_RE.match(path.name)
        if not m or not markers.LESSON_LOG_RE.match(path.name):
            continue
        by_date.setdefault(m.group(1), []).append(path)
    return by_date


def _backup(vault_root: Path, path: Path, stamp: str) -> None:
    """Copy a file into the backup dir before it is removed or overwritten."""
    backup_dir = vault_root.joinpath(*BACKUP_SUBDIR) / stamp
    backup_dir.mkdir(parents=True, exist_ok=True)
    try:
        rel = path.relative_to(vault_root)
        flat = "-".join(rel.parts)
    except ValueError:
        flat = path.name
    # Flattening can collide (`a-b/lessons-D.md` and `a/b/lessons-D.md` both
    # become `a-b-lessons-D.md`), and a silent overwrite here would break the
    # one guarantee this function exists to make. Never clobber a backup.
    dest = backup_dir / flat
    n = 1
    while dest.exists():
        dest = backup_dir / f"{flat}.{n}"
        n += 1
    shutil.copy2(str(path), str(dest))


def reconcile_lessons(vault_root: Path, dry_run: bool = False) -> dict:
    """Merge scattered lesson logs into one canonical file per date.

    Returns stats. A clean vault returns all zeros and touches nothing.
    """
    stats = {
        "scanned": 0,
        "dates": 0,
        "merged": 0,
        "entries_deduped": 0,
        "strays_removed": 0,
        "frontmatter_added": 0,
        "skipped_unsafe": 0,
        "skipped_concurrent": 0,
        "errors": 0,
    }

    canonical_dir = vault_root.joinpath(*CANONICAL_SUBDIR)
    by_date = find_logs(vault_root)
    stats["dates"] = len(by_date)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    for date_str, paths in sorted(by_date.items()):
        stats["scanned"] += len(paths)
        canonical = canonical_dir / f"lessons-{date_str}.md"

        try:
            # Canonical first so its entries win the ordering tie for identical
            # content, then the strays in stable path order.
            ordered_sources = [p for p in paths if p == canonical] + [
                p for p in paths if p != canonical
            ]
            seen: dict[str, tuple[str, str]] = {}
            had_frontmatter = False
            impostor = None
            for path in ordered_sources:
                text = path.read_text(encoding="utf-8")
                if path == canonical and text.startswith(FM_DELIM):
                    had_frontmatter = True
                # Something wearing the lesson-log filename but carrying real
                # content under no timestamped header is not a lesson log — a
                # work arc saved under this name would be rewritten down to
                # bare frontmatter, losing its cluster_id, heat and whole body.
                if _preamble_is_substantive(text):
                    impostor = path
                    break
                for header, body in split_entries(text):
                    h = entry_hash(body)
                    if h in seen:
                        stats["entries_deduped"] += 1
                        continue
                    seen[h] = (header, body)

            if impostor is not None:
                print(
                    f"WARN: lesson reconcile skipped {date_str} — "
                    f"{impostor.name} carries non-lesson content"
                )
                stats["skipped_unsafe"] += 1
                continue

            entries = sorted(seen.values(), key=lambda e: entry_sort_key(e[0]))
            new_text = render_log(date_str, entries)

            current = canonical.read_text(encoding="utf-8") if canonical.exists() else None
            strays = [p for p in paths if p != canonical]

            # Already reconciled: one file, canonical path, identical bytes.
            if current == new_text and not strays:
                continue

            # Refuse to touch a date whose merge would drop content. Skipping
            # leaves the scatter in place for that date — visible in the stats
            # and fixable — which is strictly better than deleting a lesson.
            if not merge_is_lossless(new_text, ordered_sources):
                print(f"WARN: lesson reconcile skipped {date_str} — merge would lose content")
                stats["skipped_unsafe"] += 1
                continue

            if current != new_text:
                stats["merged"] += 1
                if not had_frontmatter:
                    stats["frontmatter_added"] += 1
                if not dry_run:
                    canonical.parent.mkdir(parents=True, exist_ok=True)
                    if canonical.exists():
                        _backup(vault_root, canonical, stamp)
                    # Stage the merged text first, then re-check that the log
                    # has not moved under us, then swap — so the only remaining
                    # window is between this read and the rename below, rather
                    # than spanning the whole merge. brain-api appends to
                    # today's log at any moment and tick-drain runs every
                    # minute, so a window measured in milliseconds is worth the
                    # extra read. Yielding the date to the next tick is
                    # recoverable; a clobbered lesson is not.
                    tmp = canonical.with_suffix(".md.tmp")
                    tmp.write_text(new_text, encoding="utf-8")
                    if canonical.exists() and canonical.read_text(encoding="utf-8") != current:
                        print(
                            f"WARN: lesson reconcile skipped {date_str} — "
                            "log changed during the merge, retrying next tick"
                        )
                        stats["skipped_concurrent"] += 1
                        tmp.unlink(missing_ok=True)
                        continue
                    tmp.replace(canonical)

            for stray in strays:
                stats["strays_removed"] += 1
                if not dry_run:
                    _backup(vault_root, stray, stamp)
                    stray.unlink()

        except Exception as exc:
            # Broad by design, matching `_scan_and_collect`'s precedent in
            # brain_keeper. A single file with a stray non-UTF-8 byte raises
            # UnicodeDecodeError — a ValueError, not an OSError — and this pass
            # runs before every other phase of the tick. Letting it escape
            # wedges hot-arcs, signals, injects and dashboards too, on every
            # tick, forever, because the pass that would clean it up is the one
            # crashing. One bad date is skipped instead.
            print(f"WARN: lesson reconcile failed for {date_str}: {exc}")
            stats["errors"] += 1
            continue

    return stats
