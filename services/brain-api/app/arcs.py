from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Section(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: str = Field(min_length=1, max_length=256)
    content: str = Field(max_length=2_000_000)


class Reference(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    entity_id: str = Field(min_length=3, max_length=256)
    revision: int = Field(ge=1)
    kind: Literal["lesson", "source_range"]
    start: int | None = Field(default=None, ge=0)
    end: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def check_range(self) -> Reference:
        if self.kind == "source_range":
            if not self.entity_id.startswith("evidence:"):
                raise ValueError("source ranges must name original evidence")
            if self.start is None or self.end is None or self.end <= self.start:
                raise ValueError("source ranges need increasing character offsets")
        elif self.start is not None or self.end is not None:
            raise ValueError("lesson references cannot have range offsets")
        if self.kind == "lesson" and not self.entity_id.startswith("lesson:"):
            raise ValueError("lesson references must name lessons")
        return self


class Publication(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    entity_id: str = Field(min_length=3, max_length=256)
    revision: int = Field(ge=1)
    sections: list[Section] = Field(min_length=1, max_length=256)
    references: list[Reference] = Field(default_factory=list, max_length=1000)

    @model_validator(mode="after")
    def unique_sections(self) -> Publication:
        if len({s.id for s in self.sections}) != len(self.sections):
            raise ValueError("section ids must be unique")
        if sum(len(s.content.encode()) for s in self.sections) > 8_000_000:
            raise ValueError("document exceeds eight million bytes")
        return self


class ReadRequest(BaseModel):
    entity_id: str = Field(min_length=3, max_length=256)
    revision: int | None = Field(default=None, ge=1)
    section: str = Field(default="", max_length=256)
    section_offset: int = Field(default=0, ge=0)
    section_limit: int = Field(default=16, ge=1, le=64)
    reference_offset: int = Field(default=0, ge=0)
    reference_limit: int = Field(default=25, ge=1, le=100)
    content_hash: str = Field(default="", max_length=64)


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def document(payload: Publication, entity: dict) -> dict:
    sections = [
        {**section.model_dump(), "content_hash": digest(section.content)}
        for section in payload.sections
    ]
    body = payload.model_dump(exclude_none=True)
    return {
        "entity": entity,
        "sections": sections,
        "references": body["references"],
        "content_hash": digest(json.dumps(body, sort_keys=True, ensure_ascii=False)),
        "total_bytes": sum(len(s.content.encode("utf-8")) for s in payload.sections),
    }


def page(document: dict, section: str, offset: int, limit: int) -> dict:
    from fastapi import HTTPException

    sections = document["sections"]
    if section:
        sections = [s for s in sections if s["id"] == section]
        if not sections:
            raise HTTPException(status_code=404, detail="section not found")
    if offset >= len(sections):
        raise HTTPException(status_code=422, detail="section offset is outside document")
    selected = sections[offset : offset + limit]
    following = offset + len(selected)
    return {
        "sections": selected,
        "complete": following == len(sections),
        "next_section_offset": following if following < len(sections) else None,
        "total_sections": len(sections),
        "selected_section": section or None,
    }
