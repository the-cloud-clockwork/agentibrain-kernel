"""Task-context requests: what an authorized agent needs for its current task.

Scope comes from the credential and this brain's identity, never from request
text. Retrieved context is recalled evidence; it never outranks the task.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from collections import Counter, OrderedDict
from collections.abc import AsyncIterator, Callable
from typing import Annotated, Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from . import context_cache, graph, scope
from .retrieval import packing, rank

SCHEMA_VERSION = "2.0"
MAX_BODY_BYTES = int(os.getenv("BRAIN_CONTEXT_MAX_BODY_BYTES", "65536"))
ACCEPTED_KEPT = int(os.getenv("BRAIN_CONTEXT_ACCEPTED_KEPT", "10000"))

log = logging.getLogger("brain-api.context")

Line = Annotated[str, Field(min_length=1, max_length=1000)]
Name = Annotated[str, Field(min_length=1, max_length=128)]


class Budget(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    max_tokens: int = Field(default=3000, ge=1, le=12000)
    max_graph_depth: int = Field(default=2, ge=0, le=4)
    max_items: int = Field(default=12, ge=1, le=50)


class ContextRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: Literal["2.0"]
    request_id: str = Field(min_length=1, max_length=128)
    brain_id: str = Field(min_length=1, max_length=128)
    project_id: str = Field(min_length=1, max_length=512)
    execution_id: str = Field(min_length=1, max_length=128)
    task_id: str = Field(min_length=1, max_length=256)
    task_revision: int = Field(ge=1)
    phase: str = Field(default="", max_length=64)
    lane: Literal["eng", "ci", "plan", "master"]
    purpose: Literal[
        "task_start", "pre_write", "error_recovery", "architecture_decision", "completion"
    ]
    intent: str = Field(min_length=1, max_length=4000)
    query: str = Field(default="", max_length=2000)
    errors: list[Line] = Field(default_factory=list, max_length=8)
    subsystems: list[Name] = Field(default_factory=list, max_length=16)
    operation_class: str = Field(min_length=1, max_length=64)
    budget: Budget = Field(default_factory=Budget)
    known_context_revision: str | None = Field(default=None, max_length=128)


_failures: Counter[str] = Counter()
_accepted: OrderedDict[str, dict] = OrderedDict()
_latest: OrderedDict[tuple[str, str, str], int] = OrderedDict()
_lock = threading.Lock()
semantic_source: rank.Source | None = None
heat_source: rank.Source | None = None
cache = context_cache.ContextCache()


def enabled() -> bool:
    return os.getenv("BRAIN_TASK_CONTEXT", "true").lower() != "false"


def packing_enabled() -> bool:
    return os.getenv("BRAIN_CONTEXT_PACKING", "true").lower() != "false"


def ranking_policy() -> rank.Policy | None:
    if os.getenv("BRAIN_CONTEXT_RANKING", "true").lower() == "false":
        return None
    revision = os.getenv("BRAIN_RANKING_POLICY", rank.CURRENT)
    if revision not in rank.POLICIES:
        raise HTTPException(status_code=503, detail="BRAIN_RANKING_POLICY names no known revision")
    return rank.policy(revision)


def failures() -> dict[str, int]:
    with _lock:
        return dict(_failures)


def _reject(status: int, reason: str, error: str, **extra) -> HTTPException:
    with _lock:
        _failures[reason] += 1
        total = sum(_failures.values())
    log.warning("context_request_validation_failures=%d reason=%s", total, reason)
    return HTTPException(
        status_code=status,
        detail={
            "reason": reason,
            "error": error,
            "context_request_validation_failures": total,
            **extra,
        },
    )


async def read_bounded(chunks: AsyncIterator[bytes]) -> bytes:
    body = bytearray()
    async for chunk in chunks:
        body.extend(chunk)
        if len(body) > MAX_BODY_BYTES:
            raise _reject(413, "oversized_request", f"request exceeds {MAX_BODY_BYTES} bytes")
    return bytes(body)


def _parse(body: bytes) -> ContextRequest:
    try:
        raw = json.loads(body)
    except (ValueError, RecursionError) as exc:
        raise _reject(422, "invalid_request", "request body is not JSON") from exc
    if not isinstance(raw, dict):
        raise _reject(422, "invalid_request", "request body must be an object")
    if raw.get("schema_version") != SCHEMA_VERSION:
        raise _reject(422, "unsupported_version", f"supported schema_version is {SCHEMA_VERSION}")
    try:
        return ContextRequest.model_validate(raw)
    except ValidationError as exc:
        fields = [{"field": ".".join(map(str, e["loc"])), "type": e["type"]} for e in exc.errors()]
        raise _reject(422, "invalid_request", "request fields are invalid", fields=fields) from exc


def operation_id(request: ContextRequest, trusted: dict) -> str:
    identity = {"scope": trusted, "request": request.model_dump(exclude={"request_id"})}
    raw = json.dumps(identity, sort_keys=True, ensure_ascii=False)
    return "ctxop-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def cache_key(
    request: ContextRequest,
    trusted: dict,
    grant: frozenset[str],
    lanes: frozenset[str],
    identity: dict,
    knowledge: str,
) -> context_cache.Key:
    retrieval = request.model_dump(include={"intent", "query", "errors", "subsystems", "budget"})
    return context_cache.Key(
        brain_id=trusted["brain_id"],
        project_id=request.project_id,
        task_id=request.task_id,
        task_revision=request.task_revision,
        lane=request.lane,
        purpose=request.purpose,
        retrieval=context_cache.digest(retrieval),
        policy=context_cache.digest({**identity, "scope_policy": scope.POLICY_REVISION}),
        grant=context_cache.digest({"projects": grant, "lanes": lanes}),
        knowledge=knowledge,
    )


def _knowledge(store: graph.GraphStore | None) -> str:
    return "none" if store is None else str(store.knowledge_revision())


def _epoch(store: graph.GraphStore | None) -> tuple:
    return (scope.grants_version(), scope.POLICY_REVISION, _knowledge(store))


def _shared(key_of: Callable[[str], context_cache.Key], store, load: Callable[[], dict]) -> dict:
    if not context_cache.enabled():
        value = load()
        meta = {"state": "disabled", "negative": context_cache.negative(value), "ttl_seconds": 0}
        return {"context": value, "cache": meta}
    value, meta = cache.get(key_of(_knowledge(store)), lambda: _epoch(store), load)
    return {"context": value, "cache": meta}


def _admit(
    request: ContextRequest,
    trusted: dict,
    operation: str,
    ranked: Callable[[], dict] = dict,
) -> dict:
    task = (trusted["brain_id"], request.project_id, request.task_id)
    with _lock:
        if request.task_revision < _latest.get(task, 0):
            stale = True
        else:
            stale = False
            _latest[task] = request.task_revision
            _latest.move_to_end(task)
            while len(_latest) > ACCEPTED_KEPT:
                _latest.popitem(last=False)
            stored = _accepted.get(operation)
            if stored is not None:
                _accepted.move_to_end(operation)
                return {**stored, "replayed": True}
    if stale:
        raise _reject(409, "stale_task_revision", "a newer task revision was already accepted")
    response = {
        "schema_version": SCHEMA_VERSION,
        "operation_id": operation,
        "brain_id": trusted["brain_id"],
        "scope": trusted,
        "request": request.model_dump(exclude={"schema_version", "brain_id", "project_id"}),
        "authority": "recalled_evidence",
        "replayed": False,
    }
    response.update(ranked())
    with _lock:
        _accepted[operation] = response
        while len(_accepted) > ACCEPTED_KEPT:
            _accepted.popitem(last=False)
    return response


def accept(
    body: bytes, grant: frozenset[str], lanes: frozenset[str], deny: Callable[[], None]
) -> dict:
    if not scope.BRAIN_ID:
        raise HTTPException(status_code=503, detail="BRAIN_ID is not configured")
    store = graph.active_store()
    if context_cache.enabled():
        cache.observe(_epoch(store))
    request = _parse(body)
    if request.brain_id != scope.BRAIN_ID:
        deny()
        raise _reject(403, "brain_audience", "request names a brain other than this instance")
    if not scope.admits(grant, {request.project_id}, unattributed=False):
        deny()
        raise _reject(403, "project_grant", "project is outside this token's grant")
    if lanes and request.lane not in lanes:
        deny()
        raise _reject(403, "role_grant", "lane is outside this token's grant")
    trusted = {"brain_id": scope.BRAIN_ID, "project_ids": [request.project_id]}
    active = ranking_policy()
    if active is None:
        return _admit(request, trusted, operation_id(request, trusted))
    conservative = not packing_enabled()
    identity = {
        **trusted,
        "ranking_policy": active.revision,
        "packing": "conservative" if conservative else "full",
    }
    return _admit(
        request,
        trusted,
        operation_id(request, identity),
        lambda: _shared(
            lambda knowledge: cache_key(request, trusted, grant, lanes, identity, knowledge),
            store,
            lambda: packing.pack(
                rank.retrieve(
                    store,
                    request.project_id,
                    rank.Task(
                        f"{request.intent} {request.query}",
                        (*request.subsystems, *request.errors),
                        request.budget.max_items,
                    ),
                    active,
                    rank.Sources(semantic_source, heat_source),
                ),
                request.budget.max_tokens,
                conservative,
            ),
        ),
    )
