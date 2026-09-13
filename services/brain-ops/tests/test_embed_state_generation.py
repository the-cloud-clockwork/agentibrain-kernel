from pathlib import Path

import embed_arcs


def test_embedding_generation_change_invalidates_state(tmp_path: Path, monkeypatch):
    state_path = tmp_path / embed_arcs.STATE_FILENAME
    expected = {"left/arc.md": 123.0}
    monkeypatch.setenv(embed_arcs.STATE_GENERATION_ENV, "ollama-model-a-768")

    embed_arcs.save_state(state_path, expected)

    assert embed_arcs.load_state(state_path) == expected
    monkeypatch.setenv(embed_arcs.STATE_GENERATION_ENV, "ollama-model-b-768")
    assert embed_arcs.load_state(state_path) == {}


def test_missing_generation_invalidates_legacy_state(tmp_path: Path, monkeypatch):
    state_path = tmp_path / embed_arcs.STATE_FILENAME
    state_path.write_text('{"left/arc.md": 123.0}')
    monkeypatch.setenv(embed_arcs.STATE_GENERATION_ENV, "ollama-model-a-768")

    assert embed_arcs.load_state(state_path) == {}
