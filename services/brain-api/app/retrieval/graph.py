import os
import secrets
import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from ..graph import RELATION_TYPES, GraphReadView, GraphStore
from ..scope import admits


class ExpansionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    seed_ids: list[str] = Field(default_factory=list, max_length=16)
    project: str = Field(default="", max_length=256)
    relation_types: list[str] = Field(default_factory=lambda: list(RELATION_TYPES), max_length=16)
    max_depth: int = Field(default=2, ge=0, le=4)
    max_nodes: int = Field(default=50, ge=1, le=100)
    max_edges: int = Field(default=500, ge=1, le=1000)
    time_budget_ms: int = Field(default=100, ge=1, le=500)
    continuation: str | None = Field(default=None, max_length=128)


@dataclass
class Frontier:
    ident: str
    path: list[dict]
    direction: str = "out"
    after: tuple[str, str] = ("", "")


@dataclass
class Traversal:
    request: ExpansionRequest
    owner: tuple
    store: GraphStore
    expires: float
    revision: tuple | None = None
    nodes: list[dict] = field(default_factory=list)
    queue: deque = field(default_factory=deque)
    seen: set[str] = field(default_factory=set)
    edges: int = 0
    resumes: int = 0
    depth_limited: bool = False
    deadline: float = 0.0


_pending: OrderedDict[str, Traversal] = OrderedDict()
_pending_lock = threading.Lock()


def _visible(entity: dict | None, grant: frozenset[str], project: str) -> bool:
    return (
        entity is not None
        and admits(grant, set(entity["projects"]), unattributed=False)
        and (not project or project in entity["projects"])
    )


def _node(entity: dict, path: list[dict]) -> dict:
    explanation = "Scoped seed"
    if path:
        explanation = f"Connected to {path[0]['from']} by {len(path)} accepted relations"
    return {**entity, "path": path, "explanation": explanation}


def _start(view: GraphReadView, state: Traversal, grant: frozenset[str]) -> None:
    nodes, queue, seen = [], deque(), set()
    for ident in state.request.seed_ids:
        entity = view.get_entity(ident)
        if not _visible(entity, grant, state.request.project):
            raise HTTPException(404, "seed not found")
        if ident not in seen:
            seen.add(ident)
            nodes.append(_node(entity, []))
            queue.append(Frontier(ident, []))
    state.nodes, state.queue, state.seen = nodes, queue, seen
    state.revision = view.revision


def _step(view: GraphReadView, state: Traversal, grant: frozenset[str]) -> None:
    item = state.queue[0]
    rows = view.neighbors(item.ident, item.direction, item.after)
    if not rows:
        if item.direction == "out":
            item.direction, item.after = "in", ("", "")
        else:
            state.queue.popleft()
        return
    for edge in rows:
        if time.monotonic() >= state.deadline:
            raise TimeoutError
        if state.edges >= state.request.max_edges or len(state.nodes) >= state.request.max_nodes:
            return
        other = edge["target"] if item.direction == "out" else edge["source"]
        state.edges += 1
        if edge["status"] == "accepted" and edge["type"] in state.request.relation_types:
            entity = view.get_entity(other)
            if _visible(entity, grant, state.request.project) and other not in state.seen:
                if len(item.path) >= state.request.max_depth:
                    state.depth_limited = True
                    item.after = (edge["type"], other)
                    continue
                path = item.path + [
                    {
                        "relation_id": edge["id"],
                        "source": edge["source"],
                        "type": edge["type"],
                        "target": edge["target"],
                        "direction": item.direction,
                        "from": item.ident,
                        "to": other,
                        "revision": edge["revision"],
                        "confidence": edge["confidence"],
                    }
                ]
                state.seen.add(other)
                state.nodes.append(_node(entity, path))
                state.queue.append(Frontier(other, path))
        item.after = (edge["type"], other)


def _take(request: ExpansionRequest, owner: tuple, store: GraphStore) -> Traversal:
    now = time.monotonic()
    with _pending_lock:
        for key in list(_pending):
            if _pending[key].expires <= now:
                del _pending[key]
        state = _pending.get(request.continuation)
        if state is None or state.owner != owner or state.store is not store:
            raise HTTPException(409, "continuation unavailable; restart scoped seed retrieval")
        del _pending[request.continuation]
    state.resumes += 1
    return state


def _save(state: Traversal) -> str | None:
    if state.resumes >= 3 or time.monotonic() >= state.expires:
        return None
    key = secrets.token_urlsafe(32)
    with _pending_lock:
        while len(_pending) >= 128:
            _pending.popitem(last=False)
        _pending[key] = state
    return key


def _state(
    store: GraphStore, request: ExpansionRequest, grant: frozenset[str], actor: str
) -> Traversal:
    if any(kind not in RELATION_TYPES for kind in request.relation_types):
        raise HTTPException(422, "unknown relation type")
    owner = (actor, tuple(sorted(grant)), request.project)
    if request.continuation:
        return _take(request, owner, store)
    if not request.seed_ids or len(set(request.seed_ids)) > request.max_nodes:
        raise HTTPException(422, "provide between one and max_nodes scoped seeds")
    return Traversal(request, owner, store, time.monotonic() + 60)


def expand(store: GraphStore, request: ExpansionRequest, grant: frozenset[str], actor: str) -> dict:
    state = _state(store, request, grant, actor)
    enabled = os.getenv("BRAIN_GRAPH_EXPANSION", "false").lower() == "true"
    state.deadline = (
        time.monotonic() + min(request.time_budget_ms, state.request.time_budget_ms) / 1000
    )
    initial_count = len(state.nodes)
    reason = None
    try:
        with store.read_view(state.deadline) as view:
            if state.revision is None:
                _start(view, state, grant)
            elif state.revision != view.revision:
                raise HTTPException(409, "graph changed; restart scoped seed retrieval")
            while enabled and state.queue:
                if time.monotonic() >= state.deadline:
                    raise TimeoutError
                if len(state.nodes) >= state.request.max_nodes:
                    reason = "node_cap"
                    break
                if state.edges >= state.request.max_edges:
                    reason = "edge_cap"
                    break
                _step(view, state, grant)
    except TimeoutError:
        reason = "timeout"
    if not enabled:
        state.nodes = [node for node in state.nodes if not node["path"]]
        reason = "disabled"
    elif reason is None and state.depth_limited:
        reason = "depth_cap"
    continuation = _save(state) if reason == "timeout" else None
    return {
        "nodes": state.nodes,
        "partial": reason not in (None, "disabled"),
        "reason": reason,
        "continuation": continuation,
        "continuation_expires_in_seconds": 60 if continuation else None,
        "remaining_continuations": 3 - state.resumes if continuation else 0,
        "graph_nodes_expanded_per_request": len(state.nodes) - initial_count,
        "edges_examined": state.edges,
    }
