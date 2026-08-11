"""embed_raw — raw/ ingest notes become searchable pgvector docs."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import embed_raw  # noqa: E402


def _note(vault: Path, rel: str, text: str) -> Path:
    p = vault / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


def test_scan_raw_finds_nested_notes_skips_hidden(tmp_path):
    _note(tmp_path, "raw/inbox/2026-08-01-note.md", "hello")
    _note(tmp_path, "raw/articles/deep/piece.md", "hello")
    _note(tmp_path, "raw/inbox/_draft.md", "hidden")
    _note(tmp_path, "left/reference/lessons.md", "not raw")
    found = [str(p.relative_to(tmp_path)) for p in embed_raw.scan_raw(tmp_path)]
    assert found == ["raw/articles/deep/piece.md", "raw/inbox/2026-08-01-note.md"]


def test_build_embed_text_leads_with_title_and_source():
    fm = {"title": "Router reboot postmortem", "source": "ingest"}
    text = embed_raw.build_embed_text(fm, "DNS died after reboot.", "raw/inbox/x.md")
    assert text.startswith("Title: Router reboot postmortem")
    assert "Source: ingest" in text
    assert "DNS died" in text


def test_main_embeds_and_records_state(tmp_path, monkeypatch, capsys):
    _note(
        tmp_path,
        "raw/inbox/full-note.md",
        "This note carries enough characters to clear the fifty char noop floor easily.",
    )
    _note(tmp_path, "raw/inbox/tiny.md", "too short")
    calls = []
    monkeypatch.setattr(
        embed_raw,
        "post_embed",
        lambda url, key, payload: calls.append(payload) or {"chunks_stored": 1},
    )
    monkeypatch.setattr(sys, "argv", ["embed_raw", "--vault", str(tmp_path), "--api-key", "k"])
    assert embed_raw.main() == 0
    assert len(calls) == 1
    assert calls[0]["producer"] == "brain-raw"
    assert calls[0]["key"] == "raw:inbox:full-note.md"
    assert (tmp_path / embed_raw.STATE_FILENAME).exists()

    # Second run: unchanged file skips (diff-only).
    calls.clear()
    assert embed_raw.main() == 0
    assert calls == []


def test_prune_skipped_when_raw_dir_absent(tmp_path, monkeypatch, capsys):
    """Missing raw/ (fresh vault, transient mount) must NOT prune — an empty
    keep_keys would delete every brain-raw row server-side."""
    pruned = []
    monkeypatch.setattr(
        embed_raw.urllib.request, "urlopen", lambda *a, **k: pruned.append(a) or None
    )
    monkeypatch.setattr(
        sys, "argv", ["embed_raw", "--vault", str(tmp_path), "--api-key", "k", "--prune"]
    )
    assert embed_raw.main() == 0
    assert pruned == []
    assert "PRUNE: skipped" in capsys.readouterr().out
