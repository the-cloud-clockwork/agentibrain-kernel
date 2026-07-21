"""Tests for the brain-tick edge-accumulation + merge-runaway fixes.

Covered:
- markers.canonical_arc_id collapses .merged.merged…md chains to one id
- brain_apply.apply_merges: skips .merged.md tombstones, idempotent per pair,
  single canonical rename (no suffix stacking), strips @edge from merged body
- brain_tick_prompt.build_prompt: edge_map dedup (src,tgt) + MAX_EDGE_LINES cap,
  MAX_PROMPT_CHARS budget drops the edge map first
- scripts/vault_cleanup: collapses a synthetic chain to one file + dedups edges

Run from repo root:
    pytest -q services/brain-ops/tests
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Make brain-ops modules + scripts importable from any cwd.
_HERE = Path(__file__).resolve().parent
_BRAIN_TOOLS = _HERE.parent
_SCRIPTS = _BRAIN_TOOLS / "scripts"
for _p in (_BRAIN_TOOLS, _SCRIPTS):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import brain_apply  # noqa: E402
import brain_tick_prompt  # noqa: E402
import markers  # noqa: E402
import vault_cleanup  # noqa: E402


def _write_arc(path: Path, cluster_id: str, body: str, *, heat: int = 5) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\ncluster_id: {cluster_id}\ntitle: {cluster_id}\n"
        f"heat: {heat}\nregion: left\nstatus: active\n---\n\n{body}\n"
    )


# ---------- canonical_arc_id ----------

class TestCanonicalArcId:
    def test_collapses_chain(self):
        assert markers.canonical_arc_id("foo.md") == "foo"
        assert markers.canonical_arc_id("foo.merged.md") == "foo"
        assert markers.canonical_arc_id("foo.merged.merged.merged.md") == "foo"
        assert markers.canonical_arc_id("foo.merged.merged") == "foo"
        assert markers.canonical_arc_id("foo") == "foo"


# ---------- apply_merges guards ----------

class TestApplyMerges:
    def _vault(self, tmp_path: Path):
        d = tmp_path / "clusters" / "2026-05-01"
        return tmp_path, d

    def test_single_canonical_rename_no_stack(self, tmp_path):
        vault, d = self._vault(tmp_path)
        _write_arc(d / "arc-a.md", "arc-a", "A body")
        _write_arc(d / "arc-b.md", "arc-b", "B body")
        n = brain_apply.apply_merges(
            vault, [{"op": "merge", "arc_a": "arc-a", "arc_b": "arc-b", "title": "AB"}], dry_run=False
        )
        assert n == 1
        assert (d / "arc-b.merged.md").exists()
        assert not (d / "arc-b.merged.merged.md").exists()
        assert "## Merged from arc-b" in (d / "arc-a.md").read_text()

    def test_skips_merged_tombstone_as_arc_b(self, tmp_path):
        vault, d = self._vault(tmp_path)
        _write_arc(d / "arc-a.md", "arc-a", "A body")
        _write_arc(d / "arc-b.merged.md", "arc-b", "already a tombstone")
        n = brain_apply.apply_merges(
            vault, [{"op": "merge", "arc_a": "arc-a", "arc_b": "arc-b", "title": "AB"}], dry_run=False
        )
        assert n == 0  # tombstone is never re-merged → no runaway
        assert not (d / "arc-b.merged.merged.md").exists()

    def test_idempotent_repeated_pair(self, tmp_path):
        vault, d = self._vault(tmp_path)
        _write_arc(d / "arc-a.md", "arc-a", "A body")
        _write_arc(d / "arc-b.md", "arc-b", "B body")
        merge = [{"op": "merge", "arc_a": "arc-a", "arc_b": "arc-b", "title": "AB"}]
        brain_apply.apply_merges(vault, merge, dry_run=False)
        # Re-create arc-b (as a fresh raw file) and merge the same pair again.
        _write_arc(d / "arc-b.md", "arc-b", "B body second")
        n2 = brain_apply.apply_merges(vault, merge, dry_run=False)
        assert n2 == 0  # already folded into A
        assert (d / "arc-a.md").read_text().count("## Merged from arc-b") == 1

    def test_strips_edges_from_merged_body(self, tmp_path):
        vault, d = self._vault(tmp_path)
        _write_arc(d / "arc-a.md", "arc-a", "A body")
        _write_arc(
            d / "arc-b.md", "arc-b",
            "B body\n<!-- @edge type=related target=somewhere -->\n",
        )
        brain_apply.apply_merges(
            vault, [{"op": "merge", "arc_a": "arc-a", "arc_b": "arc-b", "title": "AB"}], dry_run=False
        )
        merged = (d / "arc-a.md").read_text()
        assert "## Merged from arc-b" in merged
        assert "@edge" not in merged  # graph markers stripped — no re-accumulation


# ---------- build_prompt edge_map dedup + caps ----------

class TestBuildPrompt:
    def test_edge_dedup_keeps_strongest_type(self, tmp_path):
        feed = tmp_path / "brain-feed"
        _write_arc(
            tmp_path / "clusters" / "2026-05-01" / "arc-e.md", "arc-e",
            "<!-- @edge type=related target=t1 -->\n<!-- @edge type=parent target=t1 -->\n",
        )
        prompt, _ = brain_tick_prompt.build_prompt(tmp_path, feed)
        assert "--parent--> t1" in prompt
        assert "--related--> t1" not in prompt

    def test_edge_map_capped(self, tmp_path):
        feed = tmp_path / "brain-feed"
        edges = "".join(
            f"<!-- @edge type=related target=t{i} -->\n"
            for i in range(brain_tick_prompt.MAX_EDGE_LINES + 25)
        )
        _write_arc(tmp_path / "clusters" / "2026-05-01" / "arc-many.md", "arc-many", edges)
        prompt, _ = brain_tick_prompt.build_prompt(tmp_path, feed)
        assert "more edges (capped)" in prompt

    def test_prompt_char_budget_drops_edge_map(self, tmp_path, monkeypatch):
        feed = tmp_path / "brain-feed"
        edges = "".join(
            f"<!-- @edge type=related target=t{i} -->\n" for i in range(40)
        )
        _write_arc(tmp_path / "clusters" / "2026-05-01" / "arc-b.md", "arc-b", edges)
        prompt0, _ = brain_tick_prompt.build_prompt(tmp_path, feed)
        # Force the budget just below the full size — the edge map is the first
        # (and here largest variable) section, so it must be dropped.
        monkeypatch.setattr(brain_tick_prompt, "MAX_PROMPT_CHARS", len(prompt0) - 50)
        prompt1, _ = brain_tick_prompt.build_prompt(tmp_path, feed)
        assert "[edge map omitted" in prompt1
        assert len(prompt1) < len(prompt0)


# ---------- vault_cleanup chain collapse ----------

class TestVaultCleanup:
    def test_collapses_chain_and_dedupes_edges(self, tmp_path):
        d = tmp_path / "clusters" / "2026-05-01"
        _write_arc(d / "foo.md", "foo", "short")
        _write_arc(d / "foo.merged.md", "foo", "medium body here")
        # Longest = survivor: duplicate edges in the base body (survive the
        # collapse) + a duplicate "## Merged from bar" section (dropped).
        _write_arc(
            d / "foo.merged.merged.md", "foo",
            "longest body with history\n"
            "<!-- @edge type=related target=x -->\n"
            "<!-- @edge type=parent target=x -->\n"
            "## Merged from bar\nbar content\n"
            "## Merged from bar\nbar content dup\n",
        )

        collapse = vault_cleanup.collapse_chains(tmp_path, dry_run=False)
        assert collapse["chains_collapsed"] == 1
        assert collapse["files_deleted"] == 2
        assert collapse["merge_notes_deduped"] == 1
        assert (d / "foo.md").exists()
        assert not (d / "foo.merged.md").exists()
        assert not (d / "foo.merged.merged.md").exists()
        # One canonical merge note survives.
        assert (d / "foo.md").read_text().count("## Merged from bar") == 1

        edges = vault_cleanup.dedupe_all_edges(tmp_path, dry_run=False)
        # related+parent → one canonical edge to x.
        assert edges["edges_removed"] >= 1
        assert (d / "foo.md").read_text().count("target=x") == 1


def test_merge_title_drops_the_models_rationale():
    """A MERGE line's title must reduce to the arc name, not carry the why.

    The model writes ``→ `new-id` — why they are the same thing``. The capture
    is anchored at end-of-line, so it used to swallow the whole justification
    and write it into the arc's `title:` frontmatter.
    """
    section = (
        "MERGE: a-arc + b-arc → `unified-id` — identical session ID, "
        "continuous work across two calendar days.\n"
        "MERGE: c-arc + d-arc → plain-id\n"
        "MERGE: e-arc + f-arc → `quoted-id`\n"
        "MERGE: g-arc + h-arc → dashed-id -- rationale after a double dash\n"
    )
    titles = [m["title"] for m in brain_apply.parse_merges(section)]
    assert titles == ["unified-id", "plain-id", "quoted-id", "dashed-id"]
    for t in titles:
        assert "`" not in t and "—" not in t


def test_merged_title_keeps_frontmatter_parseable(tmp_path):
    """A title containing a colon must not corrupt the arc's frontmatter.

    An arc whose frontmatter will not parse is skipped by the feed entirely,
    so this is a retrieval failure rather than a cosmetic one.
    """
    yaml = pytest.importorskip("yaml")
    d = tmp_path / "clusters" / "2026-07-21"
    d.mkdir(parents=True)
    (d / "a.md").write_text("---\ntitle: old\ncluster_id: a\nheat: 3\n---\n\nbody A\n")
    (d / "b.md").write_text("---\ntitle: other\ncluster_id: b\nheat: 1\n---\n\nbody B\n")

    brain_apply.apply_merges(
        tmp_path,
        [{"op": "merge", "arc_a": "a", "arc_b": "b", "title": "scope: a colon in the title"}],
        dry_run=False,
    )

    text = (d / "a.md").read_text()
    front = text.split("---")[1]
    assert yaml.safe_load(front)["title"] == "scope: a colon in the title"


def _repair_mod():
    sys.path.insert(0, str(_BRAIN_TOOLS / "scripts"))
    import repair_arc_titles
    return repair_arc_titles


def test_title_repair_spares_legitimate_subtitles():
    """An em-dash is not evidence. Only a backtick, or a long punctuated
    clause, distinguishes merge rationale from a real subtitle."""
    r = _repair_mod()
    keep = [
        "Session markers — de4d189d",
        "Brain smoke tests — 2026-04-13",
        "KB Pipeline — federated search + synthesis + dispatch",
        "Embed fallback probe — Content embedding verification",
        "GitHub Org Migration Trail: tcc-ecosystem Rename",
    ]
    for title in keep:
        assert not r.is_polluted(title), title


def test_title_repair_catches_merge_rationale():
    r = _repair_mod()
    strip = [
        "symbiosis-manifesto-canonical` — both are right-hemisphere arcs; the separate one is redundant",
        "amygdala-deploy-failed-series` — repeated deploy failures in one environment are one thread.",
        "writer-corpus-active-unified — both are writer session arcs feeding the same parents, "
        "with overlapping sibling clusters and no distinction",
    ]
    for title in strip:
        assert r.is_polluted(title), title
        cleaned = r.clean_merge_title(title)
        assert cleaned and "`" not in cleaned and "—" not in cleaned


def test_title_repair_rewrites_file_and_is_idempotent(tmp_path):
    r = _repair_mod()
    d = tmp_path / "clusters" / "2026-07-21"
    d.mkdir(parents=True)
    arc = d / "a.md"
    arc.write_text(
        "---\ntitle: unified-id` — identical session ID, continuous work, two days\n"
        "cluster_id: a\nheat: 3\n---\n\nbody stays\n"
    )
    keep = d / "b.md"
    keep.write_text("---\ntitle: Session markers — b12345\ncluster_id: b\nheat: 1\n---\n\nbody\n")

    first = r.repair(tmp_path, dry_run=False)
    assert first["repaired"] == 1
    text = arc.read_text()
    assert 'title: "unified-id"' in text
    assert "body stays" in text
    assert keep.read_text().count("Session markers — b12345") == 1

    # Running it again must be a no-op.
    assert r.repair(tmp_path, dry_run=False)["repaired"] == 0


def test_title_repair_dry_run_writes_nothing(tmp_path):
    r = _repair_mod()
    d = tmp_path / "clusters" / "2026-07-21"
    d.mkdir(parents=True)
    arc = d / "a.md"
    original = "---\ntitle: x` — both are the same thing, merged for clarity here\ncluster_id: a\n---\n\nb\n"
    arc.write_text(original)
    stats = r.repair(tmp_path, dry_run=True)
    assert stats["repaired"] == 1
    assert arc.read_text() == original
