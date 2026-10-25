from __future__ import annotations

from fastapi import HTTPException

from .graph import GraphStore, Visible


def resolve(store: GraphStore, reference: dict, visible: Visible) -> dict:
    entity_id = reference["entity_id"]
    entity = store.get_entity(entity_id)
    if entity is None or not visible(entity):
        return {"kind": reference["kind"], "availability": "unavailable"}
    detail = store.read_detail(entity_id, reference["revision"], visible)
    result = {**reference, "trust_class": "synthesized_knowledge"}
    if detail is None:
        return {**result, "availability": "detail_unavailable"}
    result.update({"availability": "available", "content_hash": detail["content_hash"]})
    if reference["kind"] == "source_range":
        result["trust_class"] = "original_evidence_reference"
        content = "".join(s["content"] for s in detail["sections"])
        if reference["end"] > len(content):
            return {**result, "availability": "range_unavailable"}
    return result


def page(
    store: GraphStore, references: list[dict], visible: Visible, offset: int, limit: int
) -> dict:
    if offset > len(references) or (offset == len(references) and offset != 0):
        raise HTTPException(status_code=422, detail="reference offset is outside document")
    selected = [resolve(store, r, visible) for r in references[offset : offset + limit]]
    following = offset + len(selected)
    return {
        "references": selected,
        "next_reference_offset": following if following < len(references) else None,
        "knowledge_reference_resolution_failures": sum(
            r["availability"] != "available" for r in selected
        ),
    }
