"""arc_id must agree with scan_arcs about what an arc is called.

scan_arcs strips ``.merged`` when deduplicating; the embed path did not when
deriving the pgvector key. A file with no ``cluster_id`` was therefore scanned
as ``X`` and stored as ``X.merged``, and nothing ever reconciled the two: the
incremental-embed state could not match the stored key, so the arc re-embedded
on every run without ever being recognised as current, and ``--prune``'s
keep_keys never contained it, so the row could not be reaped either.

The failure is silent — search still returns the arc — while the index drifts
further from the vault on every tick. That is what makes it worth a test.
"""

from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parent.parent / "embed_arcs.py"


def _load_arc_id():
    src = _SRC.read_text()
    start = src.index("def arc_id")
    end = src.index("def scan_arcs", start)
    ns: dict = {"Path": Path}
    exec(compile(src[start:end], str(_SRC), "exec"), ns)  # noqa: S102
    return ns["arc_id"]


arc_id = _load_arc_id()


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("2026-08-05-some-arc.merged.md", "2026-08-05-some-arc"),
        ("2026-08-05-some-arc.md", "2026-08-05-some-arc"),
        ("lessons-2026-07-02.md", "lessons-2026-07-02"),
    ],
)
def test_stem_drops_the_merged_marker(filename, expected):
    assert arc_id(Path("/vault/left") / filename) == expected


def test_merged_and_unmerged_resolve_to_one_id():
    """The whole point: two files, one arc, one row."""
    plain = arc_id(Path("/vault/left/arc-x.md"))
    merged = arc_id(Path("/vault/left/arc-x.merged.md"))
    assert plain == merged


def test_frontmatter_cluster_id_wins():
    assert (
        arc_id(Path("/vault/left/whatever.merged.md"), {"cluster_id": "explicit-id"})
        == "explicit-id"
    )


def test_empty_frontmatter_falls_back_to_the_stem():
    assert arc_id(Path("/vault/left/arc-y.merged.md"), {}) == "arc-y"
    assert arc_id(Path("/vault/left/arc-y.merged.md"), {"cluster_id": ""}) == "arc-y"
