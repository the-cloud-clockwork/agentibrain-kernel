import copy
import importlib
import json
from pathlib import Path

import pytest

from .test_context_ranking import _loaded
from .test_context_request import TOKENS, _post, _request
from .test_graph import _client

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "sv2_ctx03_packing.json").read_text())
SCHEMA = json.loads(
    (Path(__file__).resolve().parents[3] / "schemas" / "context-pack-v2.json").read_text()
)
SETS = ["first", "second"]


def _packing():
    return importlib.import_module("app.retrieval.packing")


def _oversized(fixture: dict) -> dict:
    spec = fixture["oversized"]
    return {
        "id": spec["id"],
        "type": "arc",
        "revision": 1,
        "projects": ["alpha"],
        "vault_ref": None,
        "title": spec["marker"] + " " + spec["phrase"] * spec["repeat"],
        "provenance": {"source": "fixture:oversized", "revision": "p1"},
    }


def _graph(name: str) -> dict:
    fixture = copy.deepcopy(FIXTURE[name])
    fixture["entities"].append(_oversized(fixture))
    return fixture


def _context(monkeypatch, vault, tmp_path, name, **env):
    fixture = _graph(name)
    db = tmp_path / f"{name}.db"
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    client = _client(monkeypatch, vault, db, TOKENS)
    store = _loaded(db, fixture)
    _sources(monkeypatch, fixture)
    return client, store, fixture


def _sources(monkeypatch, fixture):
    ctx = importlib.import_module("app.context")
    semantic = fixture["semantic"]
    monkeypatch.setattr(
        ctx, "semantic_source", lambda _q, ids: {i: semantic.get(i, 0.0) for i in ids}
    )
    monkeypatch.setattr(ctx, "heat_source", None)


def _ask(client, fixture, max_tokens=None, **changes):
    request = copy.deepcopy(fixture["request"])
    if max_tokens is not None:
        request["budget"]["max_tokens"] = max_tokens
    return _post(client, _request(**request, **changes))


def _titles(fixture: dict) -> dict:
    return {entity["id"]: entity["title"] for entity in fixture["entities"]}


def _assert_shape(pack: dict) -> None:
    assert set(pack) == set(SCHEMA["required"]) == set(SCHEMA["properties"])
    omission = SCHEMA["properties"]["omitted"]["items"]
    for entry in pack["omitted"]:
        assert set(omission["required"]) <= set(entry) <= set(omission["properties"])
        assert entry["reason"] in omission["properties"]["reason"]["enum"]
        assert set(entry["reference"]) == {"entity_id", "revision"}


def _assert_within_budget(context: dict, max_tokens: int) -> None:
    pack = context["pack"]
    tokens = _packing().tokens
    assert pack["context_pack_tokens"] == sum(tokens(item) for item in context["items"])
    assert pack["context_pack_tokens"] <= max_tokens
    assert pack["omitted_items"] == len(pack["omitted"])


@pytest.mark.parametrize("name", SETS)
def test_a_the_pack_fits_its_budget_with_complete_items_and_references(
    monkeypatch, vault, tmp_path, name
):
    client, store, fixture = _context(monkeypatch, vault, tmp_path, name)
    before = store.snapshot()
    budget = fixture["request"]["budget"]["max_tokens"]
    response = _ask(client, fixture)
    assert response.status_code == 200, response.text
    context = response.json()["context"]
    pack = context["pack"]
    _assert_shape(pack)
    _assert_within_budget(context, budget)
    titles = _titles(fixture)
    assert context["items"]
    for item in context["items"]:
        assert item["complete"] is True
        assert item["title"] == titles[item["id"]]
        assert item["authority"] == "recalled_evidence"
        assert item["reference"] == {"entity_id": item["id"], "revision": item["revision"]}
    ids = [item["id"] for item in context["items"]]
    assert fixture["small_lesson"] in ids
    kept = context["items"][ids.index(fixture["duplicate"]["kept"])]
    assert kept["supporting_source_ids"] == [
        fixture["duplicate"]["kept"],
        fixture["duplicate"]["dropped"],
    ]
    omitted = {entry["id"]: entry for entry in pack["omitted"]}
    assert omitted[fixture["duplicate"]["dropped"]]["reason"] == "duplicate"
    assert omitted[fixture["duplicate"]["dropped"]]["duplicate_of"] == fixture["duplicate"]["kept"]
    arc = omitted[fixture["oversized"]["id"]]
    assert arc["reason"] == "over_budget" and arc["tokens"] > budget
    assert arc["reference"] == {"entity_id": fixture["oversized"]["id"], "revision": 1}
    assert pack["truncated"] is True
    assert pack["unresolved_conflicts"] == sorted(fixture["conflicted"])
    assert pack["category_tokens"] == _packing().category_budgets(budget)
    assert pack["degraded"] is None and pack["authority"] == "recalled_evidence"
    assert store.snapshot() == before


@pytest.mark.parametrize("name", SETS)
def test_b_an_oversized_item_is_omitted_whole_never_cut(monkeypatch, vault, tmp_path, name):
    client, store, fixture = _context(monkeypatch, vault, tmp_path, name)
    before = store.snapshot()
    marker = fixture["oversized"]["marker"]
    for budget in (1, 50, fixture["smaller_budget"], fixture["request"]["budget"]["max_tokens"]):
        response = _ask(client, fixture, budget)
        assert response.status_code == 200, response.text
        assert marker not in response.text
        context = response.json()["context"]
        _assert_within_budget(context, budget)
        assert all(item["complete"] is True for item in context["items"])
        assert fixture["oversized"]["id"] in {entry["id"] for entry in context["pack"]["omitted"]}
        assert context["pack"]["unresolved_conflicts"] == sorted(fixture["conflicted"])
    rejected = _ask(client, fixture, 0)
    assert rejected.status_code == 422
    assert marker not in rejected.text
    assert store.snapshot() == before


@pytest.mark.parametrize("name", SETS)
def test_c_a_smaller_retry_budget_is_a_deterministic_reduced_pack_of_the_same_revision(
    monkeypatch, vault, tmp_path, name
):
    client, store, fixture = _context(monkeypatch, vault, tmp_path, name)
    full = _ask(client, fixture).json()
    before = store.snapshot()
    smaller = fixture["smaller_budget"]
    reduced = _ask(client, fixture, smaller).json()
    assert reduced["replayed"] is False
    assert reduced["operation_id"] != full["operation_id"]
    _assert_within_budget(reduced["context"], smaller)
    assert (
        reduced["context"]["pack"]["knowledge_revision"]
        == full["context"]["pack"]["knowledge_revision"]
    )
    assert (
        reduced["context"]["pack"]["context_pack_tokens"]
        <= full["context"]["pack"]["context_pack_tokens"]
    )
    assert fixture["small_lesson"] in [item["id"] for item in reduced["context"]["items"]]
    again = _ask(client, fixture, smaller).json()
    assert again["replayed"] is True and again["context"] == reduced["context"]
    restarted = _client(monkeypatch, vault, tmp_path / f"{name}.db", TOKENS)
    _sources(monkeypatch, fixture)
    after = _ask(restarted, fixture, smaller).json()
    assert after["replayed"] is False
    assert after["operation_id"] == reduced["operation_id"]
    assert after["context"] == reduced["context"]
    original = _ask(restarted, fixture).json()
    assert original["context"] == full["context"]
    assert store.snapshot() == before


@pytest.mark.parametrize("name", SETS)
def test_disabled_packing_falls_back_to_a_degraded_reference_pack(
    monkeypatch, vault, tmp_path, name
):
    client, _, fixture = _context(monkeypatch, vault, tmp_path, name)
    full = _ask(client, fixture).json()
    monkeypatch.setenv("BRAIN_CONTEXT_PACKING", "false")
    budget = fixture["request"]["budget"]["max_tokens"]
    degraded = _ask(client, fixture).json()
    assert degraded["operation_id"] != full["operation_id"]
    context = degraded["context"]
    assert context["pack"]["degraded"] == "packing_disabled"
    _assert_within_budget(context, budget)
    assert context["items"]
    for item in context["items"]:
        assert set(item) == {*_packing().REFERENCE_FIELDS, "complete", "reference"}
        assert item["complete"] is False
    assert fixture["oversized"]["marker"] not in json.dumps(degraded)
    assert context["pack"]["knowledge_revision"] == full["context"]["pack"]["knowledge_revision"]


def _item(ident, kind, title, status="current", revision=1):
    return {
        "id": ident,
        "type": kind,
        "title": title,
        "revision": revision,
        "knowledge_status": status,
        "authority": "recalled_evidence",
    }


ITEMS = [
    _item("decision:a", "decision", "Ship at noon.", "conflicted"),
    _item("decision:b", "decision", "Ship at noon!", "conflicted"),
    _item("lesson:a", "lesson", "Pin the image."),
    _item("lesson:b", "lesson", "pin the IMAGE"),
    _item("arc:a", "arc", "x" * 4000),
    _item("lesson:c", "lesson", "Rerun the failed shard."),
]


def _pack(max_tokens, items=ITEMS, conservative=False):
    return _packing().pack(
        {"items": copy.deepcopy(items), "reduced": False}, max_tokens, conservative
    )


def test_every_budget_is_respected_and_packing_is_deterministic():
    tokens = _packing().tokens
    for budget in range(1, 1300, 7):
        packed = _pack(budget)
        assert packed == _pack(budget)
        assert packed["pack"]["context_pack_tokens"] == sum(tokens(i) for i in packed["items"])
        assert packed["pack"]["context_pack_tokens"] <= budget
        ids = {item["id"] for item in packed["items"]} | {
            entry["id"] for entry in packed["pack"]["omitted"]
        }
        assert ids == {item["id"] for item in ITEMS}
        assert packed["reduced"] is False


def test_conflicted_items_are_never_merged_and_duplicates_name_their_sources():
    packed = _pack(5000)
    ids = [item["id"] for item in packed["items"]]
    assert ids == ["decision:a", "decision:b", "lesson:a", "arc:a", "lesson:c"]
    assert packed["items"][2]["supporting_source_ids"] == ["lesson:a", "lesson:b"]
    assert "supporting_source_ids" not in packed["items"][0]
    assert packed["pack"]["omitted"] == [
        {
            "id": "lesson:b",
            "type": "lesson",
            "reason": "duplicate",
            "tokens": 0,
            "reference": {"entity_id": "lesson:b", "revision": 1},
            "duplicate_of": "lesson:a",
        }
    ]
    assert packed["pack"]["truncated"] is False
    assert packed["pack"]["unresolved_conflicts"] == ["decision:a", "decision:b"]


def test_unused_category_budget_is_released_in_rank_order():
    packing = _packing()
    lessons = [_item(f"lesson:{n}", "lesson", f"Lesson number {n} text.") for n in range(4)]
    cost = packing.tokens(packing.pack({"items": lessons[:1]}, 10_000)["items"][0])
    budget = cost * 3
    assert packing.category_budgets(budget)["lesson"] < cost * 2
    packed = packing.pack({"items": lessons}, budget)
    assert [item["id"] for item in packed["items"]] == ["lesson:0", "lesson:1", "lesson:2"]
    assert [entry["id"] for entry in packed["pack"]["omitted"]] == ["lesson:3"]
    assert packed["pack"]["omitted"][0]["reason"] == "over_budget"
    assert packed["pack"]["truncated"] is True


def test_the_knowledge_revision_follows_knowledge_not_budget():
    packing = _packing()
    assert _pack(10)["pack"]["knowledge_revision"] == _pack(5000)["pack"]["knowledge_revision"]
    changed = [*ITEMS[:-1], _item("lesson:c", "lesson", "Rerun the failed shard.", revision=2)]
    assert (
        _pack(10, changed)["pack"]["knowledge_revision"] != _pack(10)["pack"]["knowledge_revision"]
    )
    assert packing.knowledge_revision([]).startswith("kr-")


def test_an_empty_context_packs_to_an_empty_pack():
    packed = _pack(100, [])
    assert packed["items"] == [] and packed["pack"]["omitted"] == []
    assert packed["pack"]["context_pack_tokens"] == 0
    assert packed["pack"]["truncated"] is False


def test_claims_differing_beyond_case_and_punctuation_stay_distinct():
    items = [
        _item("decision:ja", "decision", "日本語の決定"),
        _item("decision:zh", "decision", "中文の決定"),
        _item("lesson:yes", "lesson", "Ship ✓"),
        _item("lesson:no", "lesson", "Ship ✗"),
        _item("lesson:dots", "lesson", "..."),
        _item("lesson:bang", "lesson", "!!!"),
    ]
    packed = _pack(5000, items)
    assert [item["id"] for item in packed["items"]] == [item["id"] for item in items]
    assert packed["pack"]["omitted"] == []
