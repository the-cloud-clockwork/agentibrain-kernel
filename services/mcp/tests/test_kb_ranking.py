"""Tests for kb_search's hybrid ranking.

The two sources score on unrelated scales. The vault's is a match heuristic —
capped line count plus flat filename and all-token bonuses, so 30-40 for a file
that merely repeats a common word. The embeddings' is cosine similarity, where
0.33 is a good match. Normalizing the vault scores against their own batch max
forced the best lexical hit to 1.0 however weak it was, so lexical always won:
a search for the verbatim text of a lesson written that day returned four
unrelated documents and never the lesson.

Reciprocal Rank Fusion uses only each source's internal ordering, so the scales
never meet.

Run with `pytest services/mcp/tests/`.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import sys
from pathlib import Path

import pytest


def _kb():
    app_dir = Path(__file__).resolve().parents[1] / "app"
    if str(app_dir) not in sys.path:
        sys.path.insert(0, str(app_dir))
    return importlib.import_module("tools.kb")


class _Recorder:
    """Captures registered tool functions from a fake FastMCP."""

    def __init__(self):
        self.tools = {}

    def tool(self, *_a, **_k):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn

        return deco


def _kb_search(monkeypatch, obsidian, artifact):
    """Register kb_search with both sources faked out."""
    kb = _kb()

    async def fake_vault(query, limit):
        return list(obsidian)

    async def fake_embed(query, limit, min_score, producer=""):
        fake_embed.producer = producer
        return list(artifact)

    fake_embed.producer = None
    monkeypatch.setattr(kb, "_search_vault", fake_vault)
    monkeypatch.setattr(kb, "_search_embeddings", fake_embed)
    rec = _Recorder()
    kb.register(rec)
    return rec.tools["kb_search"], kb, fake_embed


def _obs(ref, score):
    return {"source": "obsidian", "ref": ref, "title": ref, "score": score, "preview": ""}


def _art(ref, score):
    return {"source": "artifact", "ref": ref, "title": ref, "score": score, "preview": ""}


def test_strong_semantic_hit_is_not_buried_by_weak_lexical(monkeypatch):
    """The original failure, as a regression guard.

    Four unrelated lexical hits at 30-40 against the one file that actually
    contains the sentence, at cosine 0.33.
    """
    obsidian = [_obs(f"noise-{i}.md", s) for i, s in enumerate((38, 35, 32, 30))]
    artifact = [_art("lessons-2026-08-11", 0.33)]
    search, _kbmod, _ = _kb_search(monkeypatch, obsidian, artifact)

    out = json.loads(asyncio.run(search(query="refused TCP connection", limit=10)))
    refs = [r["ref"] for r in out["results"]]

    assert "lessons-2026-08-11" in refs
    # Rank 1 of each source ties on RRF, and the lesson must not sit below all
    # four lexical hits the way it did before.
    assert refs.index("lessons-2026-08-11") <= 1


def test_single_weak_hit_is_not_inflated(monkeypatch):
    """Min-max gave a lone hit 1.0 regardless of quality. RRF ranks by position."""
    search, kb, _ = _kb_search(monkeypatch, [_obs("only.md", 3)], [])

    out = json.loads(asyncio.run(search(query="q", limit=10)))

    assert out["results"][0]["normalized_score"] == pytest.approx(1.0 / (kb.RRF_K + 1))
    assert out["results"][0]["normalized_score"] < 1.0
    assert out["results"][0]["source_rank"] == 1


def test_empty_source_does_not_break_ranking(monkeypatch):
    search, _kbmod, _ = _kb_search(monkeypatch, [], [_art("a", 0.5), _art("b", 0.4)])

    out = json.loads(asyncio.run(search(query="q", limit=10)))

    assert [r["ref"] for r in out["results"]] == ["a", "b"]


def test_equal_scores_keep_deterministic_order(monkeypatch):
    obsidian = [_obs("x.md", 20), _obs("y.md", 20), _obs("z.md", 20)]
    search, _kbmod, _ = _kb_search(monkeypatch, obsidian, [])

    first = json.loads(asyncio.run(search(query="q", limit=10)))
    second = json.loads(asyncio.run(search(query="q", limit=10)))

    assert [r["ref"] for r in first["results"]] == ["x.md", "y.md", "z.md"]
    assert [r["ref"] for r in first["results"]] == [r["ref"] for r in second["results"]]


def test_raw_score_is_preserved_for_debuggability(monkeypatch):
    search, _kbmod, _ = _kb_search(monkeypatch, [_obs("a.md", 37)], [_art("b", 0.41)])

    out = json.loads(asyncio.run(search(query="q", limit=10)))
    by_ref = {r["ref"]: r for r in out["results"]}

    assert by_ref["a.md"]["score"] == 37
    assert by_ref["b"]["score"] == 0.41


def test_producer_is_only_sent_when_requested(monkeypatch):
    search, _kbmod, fake_embed = _kb_search(monkeypatch, [], [_art("a", 0.5)])

    asyncio.run(search(query="q", limit=10))
    assert fake_embed.producer == ""

    asyncio.run(search(query="q", limit=10, producer="brain-lesson"))
    assert fake_embed.producer == "brain-lesson"


def test_rank_one_tie_resolves_to_semantic(monkeypatch):
    """Rank 1 of each source ties; the tiebreak must be deliberate.

    Leaving it to the order tasks were appended in makes the top result an
    implementation detail of asyncio.gather.
    """
    search, _kbmod, _ = _kb_search(
        monkeypatch, [_obs("keyword.md", 40)], [_art("semantic", 0.2)]
    )

    out = json.loads(asyncio.run(search(query="q", limit=10)))

    assert out["results"][0]["ref"] == "semantic"
    assert out["results"][0]["normalized_score"] == out["results"][1]["normalized_score"]
