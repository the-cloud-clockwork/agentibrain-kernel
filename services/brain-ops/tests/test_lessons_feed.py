"""Tests for brain-feed/lessons.md.

The tick counted lessons for months and wrote no feed, so nothing a session saw
ever mentioned them. This is the surface that fixes that: recency-ordered,
capped, and swept of anything past the staleness window, in the same shape as
signals.md and inject.md.

`id: lessons` matters and is asserted: brain-api's feed_payload() buckets by id
substring, so an id containing "hot" or "inject" would land in the wrong bucket.

Run from repo root:
    pytest -q services/brain-ops/tests
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_BRAIN_TOOLS = _HERE.parent
if str(_BRAIN_TOOLS) not in sys.path:
    sys.path.insert(0, str(_BRAIN_TOOLS))

import brain_keeper  # noqa: E402

NOW = datetime.now(timezone.utc)


def _vault_with(tmp_path: Path, entries: list[tuple[datetime, str]]) -> Path:
    vault = tmp_path / "vault"
    ref = vault / "left" / "reference"
    ref.mkdir(parents=True)
    by_date: dict[str, list[tuple[datetime, str]]] = {}
    for ts, body in entries:
        by_date.setdefault(ts.strftime("%Y-%m-%d"), []).append((ts, body))
    for date_str, items in by_date.items():
        text = "".join(f"## {ts.isoformat()}\n\n{body}\n\n" for ts, body in items)
        (ref / f"lessons-{date_str}.md").write_text(text)
    return vault


def test_empty_vault_writes_placeholder(tmp_path: Path):
    vault = tmp_path / "vault"
    (vault / "left" / "reference").mkdir(parents=True)
    out = tmp_path / "lessons.md"

    stats = brain_keeper.write_lessons_feed(out, vault, now=NOW)

    text = out.read_text()
    assert text.startswith("---\n")
    assert "id: lessons" in text
    assert "No recent lessons." in text
    assert stats["written"] == 0


def test_newest_first_and_capped(tmp_path: Path):
    entries = [
        (NOW - timedelta(hours=i), f"Lesson number {i}.")
        for i in range(brain_keeper.BRAIN_LESSON_FEED_MAX + 4)
    ]
    vault = _vault_with(tmp_path, entries)
    out = tmp_path / "lessons.md"

    stats = brain_keeper.write_lessons_feed(out, vault, now=NOW)

    assert stats["written"] == brain_keeper.BRAIN_LESSON_FEED_MAX
    text = out.read_text()
    assert "Lesson number 0." in text
    # The oldest entries fall off the cap.
    assert f"Lesson number {brain_keeper.BRAIN_LESSON_FEED_MAX + 3}." not in text
    # Newest first: entry 0 precedes entry 1 in the rendered feed.
    assert text.index("Lesson number 0.") < text.index("Lesson number 1.")


def test_stale_entries_are_swept(tmp_path: Path):
    fresh = (NOW - timedelta(days=1), "Fresh lesson.")
    stale = (
        NOW - timedelta(days=brain_keeper.BRAIN_STALE_LESSON_DAYS + 5),
        "Ancient lesson.",
    )
    vault = _vault_with(tmp_path, [fresh, stale])
    out = tmp_path / "lessons.md"

    stats = brain_keeper.write_lessons_feed(out, vault, now=NOW)

    assert stats["tombstoned_stale"] == 1
    text = out.read_text()
    assert "Fresh lesson." in text
    assert "Ancient lesson." not in text


def test_id_does_not_collide_with_feed_buckets(tmp_path: Path):
    """brain-api buckets feed entries by id substring — 'hot'/'inject' misroute."""
    vault = _vault_with(tmp_path, [(NOW, "A lesson.")])
    out = tmp_path / "lessons.md"

    brain_keeper.write_lessons_feed(out, vault, now=NOW)

    fm = out.read_text().split("---")[1]
    feed_id = next(line.split(":", 1)[1].strip() for line in fm.splitlines() if line.startswith("id:"))
    assert "hot" not in feed_id
    assert "inject" not in feed_id


def test_untimestamped_block_is_not_treated_as_an_entry(tmp_path: Path):
    """A block with no ISO timestamp in its header is not a lesson entry.

    The boundary requires a timestamp so that a `## ` line inside a lesson's
    own prose cannot split it. The cost is that a malformed header is invisible
    to the feed — acceptable because brain-api always writes a timestamp, and
    because the reconcile pass refuses to rewrite any file whose merge would
    drop such a block rather than deleting it (see merge_is_lossless).
    """
    vault = tmp_path / "vault"
    ref = vault / "left" / "reference"
    ref.mkdir(parents=True)
    (ref / "lessons-2026-08-10.md").write_text("## not-a-timestamp\n\nOrphan block.\n")
    out = tmp_path / "lessons.md"

    stats = brain_keeper.write_lessons_feed(out, vault, now=NOW)

    assert stats["written"] == 0
    assert "No recent lessons." in out.read_text()


def test_orders_chronologically_not_by_string(tmp_path: Path):
    """Ordering must survive a header whose offset is not UTC.

    ISO strings only sort as time when every header shares one offset. That is
    true today because brain-api normalizes to UTC, so this guards a landmine
    rather than a live break: a hand-edited entry or a future producer in
    another zone would otherwise silently mis-order "most recent".
    """
    vault = tmp_path / "vault"
    ref = vault / "left" / "reference"
    ref.mkdir(parents=True)
    today = NOW.strftime("%Y-%m-%d")
    earlier = NOW - timedelta(hours=3)
    later = NOW - timedelta(hours=1)
    # `later` is chronologically newer but sorts EARLIER as a raw string,
    # because its wall-clock reads smaller under a negative offset.
    shifted = later.astimezone(timezone(timedelta(hours=-5)))
    (ref / f"lessons-{today}.md").write_text(
        f"## {earlier.isoformat()}\n\nOlder lesson.\n\n"
        f"## {shifted.isoformat()}\n\nNewer lesson.\n\n"
    )
    out = tmp_path / "lessons.md"

    brain_keeper.write_lessons_feed(out, vault, now=NOW)

    text = out.read_text()
    assert text.index("Newer lesson.") < text.index("Older lesson.")
