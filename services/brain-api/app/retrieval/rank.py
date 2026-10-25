"""Explainable task-context ranking under a versioned scoring policy.

Scope is applied before any candidate is scored. Heat never admits a candidate:
only lexical, task, semantic or graph evidence does. A failed optional source is
labelled and left out of the score rather than replaced by an invented value.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from .current_knowledge import select_current

log = logging.getLogger("brain-api.rank")

POLICIES = {
    "1": {
        "lexical": 0.5,
        "task": 0.3,
        "semantic": 0.0,
        "graph": 0.2,
        "heat": 0.05,
        "inferred": 0.5,
    },
    "2": {
        "lexical": 0.4,
        "task": 0.25,
        "semantic": 0.2,
        "graph": 0.15,
        "heat": 0.05,
        "inferred": 0.4,
    },
}
CURRENT = "2"
DIRECT = ("lexical", "task", "semantic")
TYPES = frozenset({"arc", "lesson", "decision"})
STATUS_WEIGHT = {
    "current": 1.0,
    "unverified": 0.8,
    "review_required": 0.8,
    "conflicted": 0.5,
    "superseded": 0.3,
}
SCAN_LIMIT = 2000
EDGE_BUDGET = 2000
_WORD = re.compile(r"[a-z0-9]{3,}")

Source = Callable[[str, list[str]], dict[str, float]]


@dataclass(frozen=True)
class Task:
    query: str
    terms: tuple[str, ...] = ()
    limit: int = 12


@dataclass(frozen=True)
class Sources:
    semantic: Source | None = None
    heat: Source | None = None


NO_SOURCES = Sources()


def policy(revision: str) -> dict:
    return {"revision": revision, **POLICIES[revision]}


def terms(*texts: str) -> frozenset[str]:
    return frozenset(word for text in texts for word in _WORD.findall(text.lower()))


def _overlap(wanted: frozenset[str], have: frozenset[str]) -> tuple[float, list[str]]:
    shared = sorted(wanted & have)
    return (len(shared) / len(wanted) if wanted else 0.0), shared


def _optional(name: str, source: Source | None, query: str, ids: list[str]) -> tuple:
    if source is None:
        return None, "not_configured"
    try:
        values = source(query, ids)
        return {ident: min(1.0, max(0.0, float(values.get(ident, 0.0)))) for ident in ids}, "ok"
    except Exception:
        log.warning("optional ranking source %s failed", name)
        return None, "unavailable"


def _direct(candidate: dict, query: frozenset, task: frozenset, semantic, active: dict) -> tuple:
    text = terms(candidate["title"], candidate.get("summary", ""))
    lexical, lexical_terms = _overlap(query, text)
    named, task_terms = _overlap(task, text)
    components = {
        "lexical": lexical,
        "task": named,
        "semantic": None if semantic is None else semantic[candidate["id"]],
    }
    reasons = []
    if lexical_terms:
        reasons.append("matches query terms " + ", ".join(lexical_terms))
    if task_terms:
        reasons.append("names task terms " + ", ".join(task_terms))
    if components["semantic"]:
        reasons.append(f"semantic similarity {components['semantic']:.2f}")
    relevance = sum(active[k] * v for k, v in components.items() if v is not None)
    return components, relevance, reasons


def _graph(edges: list[dict], relevance: dict[str, float], active: dict) -> dict[str, dict]:
    best: dict[str, dict] = {}
    for edge in edges:
        if edge["status"] != "accepted":
            continue
        factor = active["inferred"] if edge["confidence"] == "inferred" else 1.0
        for near, far in ((edge["source"], edge["target"]), (edge["target"], edge["source"])):
            value = relevance.get(near, 0.0) * factor
            if value > 0 and value > best.get(far, {}).get("value", 0.0):
                best[far] = {
                    "value": value,
                    "relation_id": edge["id"],
                    "type": edge["type"],
                    "from": near,
                    "confidence": edge["confidence"],
                }
    return best


def rank(
    task: Task,
    candidates: list[dict],
    edges: list[dict],
    active: dict,
    sources: Sources = NO_SOURCES,
) -> dict:
    ids = [candidate["id"] for candidate in candidates]
    wanted, named = terms(task.query), terms(*task.terms)
    semantic_values, semantic_state = _optional("semantic", sources.semantic, task.query, ids)
    heat_values, heat_state = _optional("heat", sources.heat, task.query, ids)
    direct = {c["id"]: _direct(c, wanted, named, semantic_values, active) for c in candidates}
    graph = _graph(edges, {ident: value[1] for ident, value in direct.items()}, active)
    items, excluded = [], {"heat_only": 0, "no_task_evidence": 0}
    for candidate in candidates:
        ident = candidate["id"]
        components, direct_relevance, reasons = direct[ident]
        relevance = direct_relevance
        link = graph.get(ident)
        components = {**components, "graph": link["value"] if link else 0.0}
        relevance += active["graph"] * components["graph"]
        components["heat"] = None if heat_values is None else heat_values[ident]
        if relevance <= 0:
            excluded["heat_only" if components["heat"] else "no_task_evidence"] += 1
            continue
        status = candidate.get("knowledge_status", "unverified")
        score = relevance * STATUS_WEIGHT[status] + active["heat"] * (components["heat"] or 0.0)
        path = []
        if link:
            path = [{k: link[k] for k in ("relation_id", "type", "from", "confidence")}]
            reasons.append(f"{link['type']} relation to {link['from']} ({link['confidence']})")
        reasons.append(f"knowledge status {status}")
        if components["heat"]:
            reasons.append(f"heat {components['heat']:.2f} adjusts order only")
        items.append(
            {
                "id": ident,
                "type": candidate["type"],
                "title": candidate["title"],
                "knowledge_status": status,
                "evidence": "graph" if direct_relevance <= 0 else "direct",
                "confidence": link["confidence"] if link else None,
                "approval": "accepted" if link else None,
                "authority": "recalled_evidence",
                "score": round(score, 6),
                "components": {
                    k: None if v is None else round(v, 6) for k, v in components.items()
                },
                "reasons": reasons,
                "path": path,
            }
        )
    items.sort(key=lambda item: (-item["score"], item["id"]))
    sources = {"semantic": semantic_state, "heat": heat_state}
    return {
        "policy_revision": active["revision"],
        "authority": "recalled_evidence",
        "items": items[: task.limit],
        "excluded": excluded,
        "sources": sources,
        "reduced": "unavailable" in sources.values(),
        "score_basis": [k for k in (*DIRECT, "graph", "heat") if sources.get(k, "ok") == "ok"],
    }


def _edges(view, ids: set[str], budget: int) -> tuple[list[dict], bool]:
    edges, examined = [], 0
    for ident in sorted(ids):
        after = ("", "")
        while rows := view.neighbors(ident, "out", after):
            for edge in rows:
                examined += 1
                if examined > budget:
                    return edges, False
                after = (edge["type"], edge["target"])
                if edge["target"] in ids:
                    edges.append(edge)
    return edges, True


def _unavailable(state: str, active: dict) -> dict:
    return {
        "policy_revision": active["revision"],
        "authority": "recalled_evidence",
        "items": [],
        "excluded": {"heat_only": 0, "no_task_evidence": 0},
        "sources": {"graph": state},
        "reduced": state == "unavailable",
        "score_basis": [],
    }


def retrieve(
    store,
    project: str,
    task: Task,
    active: dict,
    sources: Sources = NO_SOURCES,
    time_budget: float = 0.25,
) -> dict:
    if store is None:
        return _unavailable("not_configured", active)
    try:
        with store.read_view(time.monotonic() + time_budget) as view:
            nodes = view.entities(project, TYPES, SCAN_LIMIT)
            ids = {node["id"] for node in nodes}
            edges, edges_complete = _edges(view, ids, EDGE_BUDGET)
            labelled = select_current(
                view, nodes, lambda e: e is not None and project in e["projects"], EDGE_BUDGET
            )["nodes"]
    except (TimeoutError, sqlite3.Error):
        log.warning("graph candidate source failed")
        return _unavailable("unavailable", active)
    result = rank(task, labelled, edges, active, sources)
    result["sources"]["graph"] = "ok" if edges_complete else "partial"
    result["reduced"] = result["reduced"] or not edges_complete
    result["scan_limited"] = len(nodes) >= SCAN_LIMIT
    return result


def context_relevance_at_k(items: list[dict], relevant: Iterable[str], k: int) -> float:
    wanted = set(relevant)
    if not wanted or k < 1:
        return 0.0
    hits = sum(1 for item in items[:k] if item["id"] in wanted)
    return hits / min(k, len(wanted))
