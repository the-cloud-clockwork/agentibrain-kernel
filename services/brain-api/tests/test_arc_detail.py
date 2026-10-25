import hashlib

import pytest

from .test_graph import ROUTER, _auth, _client


def _entity(ident="arc:long", revision=1, project="alpha"):
    return {
        "id": ident,
        "type": ident.split(":")[0],
        "revision": revision,
        "projects": [project],
        "vault_ref": ident,
        "title": ident,
        "provenance": {"source": "fixture", "revision": str(revision)},
    }


def _publish(client, revision=1, sections=None, **changes):
    record = _entity(revision=revision)
    assert client.post("/graph/entity", json=record, headers=_auth(ROUTER)).status_code == 201
    payload = {
        "entity_id": record["id"],
        "revision": revision,
        "sections": sections
        or [
            {"id": "first", "content": "α long body\n" * 1000},
            {"id": "second", "content": "last complete section\n"},
        ],
        "references": [],
        **changes,
    }
    response = client.post("/knowledge/detail", json=payload, headers=_auth(ROUTER))
    assert response.status_code == 201, response.text
    return payload, response.json()


def _read(client, **params):
    return client.get(
        "/knowledge/detail", params={"entity_id": "arc:long", **params}, headers=_auth("worker")
    )


@pytest.mark.parametrize("instance", [1, 2])
def test_complete_sections_and_explicit_pagination(monkeypatch, vault, tmp_path, instance):
    client = _client(monkeypatch, vault, tmp_path / f"detail{instance}.db", "hive-a@alpha=worker")
    payload, published = _publish(client)
    first = _read(client, section_limit=1).json()
    assert first["revision"] == 1
    assert first["content_hash"] == published["content_hash"]
    assert first["sections"] == [
        {
            **payload["sections"][0],
            "content_hash": hashlib.sha256(payload["sections"][0]["content"].encode()).hexdigest(),
        }
    ]
    assert first["complete"] is False
    second = _read(
        client, revision=1, section_offset=first["next_section_offset"], section_limit=1
    ).json()
    assert second["sections"][0]["content"] == payload["sections"][1]["content"]
    assert second["complete"] is True
    assert second["next_section_offset"] is None
    selected = _read(client, revision=1, section="first").json()
    assert selected["sections"] == first["sections"]
    assert selected["complete"] is True
    assert selected["trust_class"] == "synthesized_knowledge"


def test_historical_revision_replay_and_restriction(monkeypatch, vault, tmp_path):
    db = tmp_path / "history.db"
    client = _client(monkeypatch, vault, db, "hive-a@alpha=worker")
    payload, published = _publish(client)
    before = _read(client, revision=1).json()
    _publish(client, 2, [{"id": "new", "content": "new revision"}])
    client = _client(monkeypatch, vault, db, "hive-a@alpha=worker")
    assert _read(client, revision=1).json() == before
    assert _read(client).json()["revision"] == 2
    replay = client.post("/knowledge/detail", json=payload, headers=_auth(ROUTER))
    assert replay.status_code == 201
    assert replay.json()["action"] == "replayed"
    assert replay.json()["content_hash"] == published["content_hash"]
    payload["sections"][0]["content"] = "rewrite"
    assert client.post("/knowledge/detail", json=payload, headers=_auth(ROUTER)).status_code == 409
    assert _read(client, revision=1).json() == before
    assert _read(client, revision=9).status_code == 404
    restricted = _entity(revision=3, project="beta")
    assert client.post("/graph/entity", json=restricted, headers=_auth(ROUTER)).status_code == 201
    response = _read(client, revision=1)
    assert response.status_code == 404
    assert "long body" not in response.text


def test_missing_source_range_and_reference_boundary(monkeypatch, vault, tmp_path):
    client = _client(monkeypatch, vault, tmp_path / "sources.db", "hive-a@alpha=worker")
    lesson = _entity("lesson:attached")
    assert client.post("/graph/entity", json=lesson, headers=_auth(ROUTER)).status_code == 201
    refs = [
        {"entity_id": "lesson:attached", "revision": 1, "kind": "lesson"},
        {
            "entity_id": "evidence:missing",
            "revision": 1,
            "kind": "source_range",
            "start": 1,
            "end": 4,
        },
    ]
    _publish(client, references=refs)
    first = _read(client, reference_limit=1).json()
    assert first["references"][0]["entity_id"] == "lesson:attached"
    assert first["references"][0]["availability"] == "detail_unavailable"
    assert first["next_reference_offset"] == 1
    second = _read(client, revision=1, reference_offset=1, reference_limit=1).json()
    assert second["references"] == [{"kind": "source_range", "availability": "unavailable"}]
    assert second["next_reference_offset"] is None
    assert second["knowledge_reference_resolution_failures"] == 1


def test_refusals_and_rollback_keep_authoritative_bytes(monkeypatch, vault, tmp_path):
    client = _client(monkeypatch, vault, tmp_path / "denial.db", "hive-a@alpha=worker")
    payload, _ = _publish(client)
    before = _read(client, revision=1).json()
    assert (
        client.post("/knowledge/detail", json=payload, headers=_auth("worker")).status_code == 403
    )
    for params in [{"section": "absent"}, {"section_offset": 3}, {"content_hash": "bad"}]:
        assert _read(client, **params).status_code in (404, 409, 422)
    assert _read(client, section_limit=0).status_code == 422
    assert _read(client, revision=0).status_code == 422
    monkeypatch.setenv("BRAIN_FULL_DETAIL", "false")
    assert _read(client).status_code == 503
    monkeypatch.setenv("BRAIN_FULL_DETAIL", "true")
    assert _read(client, revision=1).json() == before


def test_available_lesson_and_source_ranges_resolve_exact_revisions(monkeypatch, vault, tmp_path):
    client = _client(monkeypatch, vault, tmp_path / "references.db", "hive-a@alpha=worker")
    for ident, text in [
        ("lesson:attached", "complete lesson"),
        ("evidence:records", "one\ntwo\nthree"),
    ]:
        entity = _entity(ident)
        assert client.post("/graph/entity", json=entity, headers=_auth(ROUTER)).status_code == 201
        payload = {"entity_id": ident, "revision": 1, "sections": [{"id": "body", "content": text}]}
        assert (
            client.post("/knowledge/detail", json=payload, headers=_auth(ROUTER)).status_code == 201
        )
    refs = [
        {"entity_id": "lesson:attached", "revision": 1, "kind": "lesson"},
        {
            "entity_id": "evidence:records",
            "revision": 1,
            "kind": "source_range",
            "start": 4,
            "end": 8,
        },
        {
            "entity_id": "evidence:records",
            "revision": 1,
            "kind": "source_range",
            "start": 4,
            "end": 999,
        },
    ]
    _publish(client, references=refs)
    result = _read(client).json()
    assert [r["availability"] for r in result["references"]] == [
        "available",
        "available",
        "range_unavailable",
    ]
    assert result["knowledge_reference_resolution_failures"] == 1
    ref = result["references"][1]
    source = client.get(
        "/knowledge/detail",
        params={
            "entity_id": ref["entity_id"],
            "revision": ref["revision"],
            "content_hash": ref["content_hash"],
        },
        headers=_auth("worker"),
    ).json()
    assert source["trust_class"] == "original_evidence"
    assert source["sections"][0]["content"][ref["start"] : ref["end"]] == "two\n"


@pytest.mark.parametrize(
    "changes",
    [
        {"sections": [{"id": "same", "content": "a"}, {"id": "same", "content": "b"}]},
        {"sections": [], "text_preview": "excerpt"},
        {
            "references": [
                {
                    "entity_id": "evidence:x",
                    "revision": 1,
                    "kind": "source_range",
                    "start": 3,
                    "end": 1,
                }
            ]
        },
        {
            "references": [
                {
                    "entity_id": "lesson:x",
                    "revision": 1,
                    "kind": "source_range",
                    "start": 0,
                    "end": 1,
                }
            ]
        },
    ],
)
def test_invalid_publications_never_replace_accepted_document(
    monkeypatch, vault, tmp_path, changes
):
    client = _client(monkeypatch, vault, tmp_path / "invalid.db", "hive-a@alpha=worker")
    payload, _ = _publish(client)
    before = _read(client).json()
    response = client.post("/knowledge/detail", json={**payload, **changes}, headers=_auth(ROUTER))
    assert response.status_code == 422
    assert _read(client).json() == before
