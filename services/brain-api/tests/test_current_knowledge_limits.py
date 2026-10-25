import pytest

from tests.test_current_knowledge import _edge, _read, _setup


@pytest.mark.parametrize(
    "limits", [{"max_depth": 0}, {"max_nodes": 1}, {"relation_types": []}, {"max_edges": 1}]
)
def test_incomplete_replacement_context_never_claims_current_guidance(
    monkeypatch, vault, tmp_path, limits
):
    client = _setup(monkeypatch, vault, tmp_path)
    assert _edge(client, "decision:approved", "supersedes", "lesson:old").status_code == 201
    result = _read(client, **limits)
    assert result["guidance"] == []
    assert result["selection_complete"] is False
    assert result["nodes"][0]["knowledge_status"] == "unverified"


def test_inverse_direction_selects_the_replacement(monkeypatch, vault, tmp_path):
    client = _setup(monkeypatch, vault, tmp_path)
    assert _edge(client, "lesson:old", "superseded_by", "decision:approved").status_code == 201
    result = _read(client)
    assert [node["id"] for node in result["guidance"]] == ["decision:approved"]
    assert result["history"][0]["id"] == "lesson:old"


def test_competing_replacements_are_not_ranked_arbitrarily(monkeypatch, vault, tmp_path):
    client = _setup(monkeypatch, vault, tmp_path)
    for source in ("lesson:opposite", "decision:approved"):
        assert _edge(client, source, "supersedes", "lesson:old").status_code == 201
    result = _read(client)
    assert result["guidance"] == []
    assert result["conflicts"][0]["kind"] == "competing_replacements"
