import os

from app.graph import GraphReadView


def _assertions(view: GraphReadView, nodes: list[dict], visible, budget: int) -> tuple:
    ids = {node["id"] for node in nodes}
    edges = {}
    examined = 0
    complete = True
    for node in nodes:
        if node["type"] not in {"lesson", "decision"}:
            continue
        for direction in ("out", "in"):
            after = ("", "")
            while True:
                rows = view.neighbors(node["id"], direction, after)
                if not rows:
                    break
                for edge in rows:
                    if examined >= budget:
                        return list(edges.values()), examined, False
                    examined += 1
                    other = edge["target"] if direction == "out" else edge["source"]
                    after = (edge["type"], other)
                    if edge["status"] != "accepted" or edge["type"] not in {
                        "supersedes",
                        "contradicts",
                    }:
                        continue
                    if edge["type"] == "supersedes" and edge["confidence"] != "observed":
                        continue
                    if not visible(view.get_entity(other)):
                        continue
                    if other not in ids:
                        complete = False
                    else:
                        edges[edge["id"]] = edge
    return list(edges.values()), examined, complete


def _roots(ident: str, parents: dict[str, set[str]], cache: dict, trail: tuple = ()) -> set[str]:
    if ident in trail:
        raise ValueError(tuple(sorted(trail[trail.index(ident) :])))
    if ident not in cache:
        result = set()
        for parent in sorted(parents[ident]):
            result.update(_roots(parent, parents, cache, (*trail, ident)))
        cache[ident] = result if parents[ident] else {ident}
    return cache[ident]


def _conflicts(parents: dict, edges: list[dict]) -> tuple:
    roots, cache, cycles = {}, {}, set()
    for ident in parents:
        try:
            roots[ident] = _roots(ident, parents, cache)
        except ValueError as exc:
            cycles.add(exc.args[0])
    conflicts = [
        {"kind": "supersession_cycle", "entities": list(cycle)} for cycle in sorted(cycles)
    ]
    blocked = set()
    for ident, replacements in roots.items():
        if len(replacements) > 1:
            blocked.update(replacements)
            conflicts.append(
                {
                    "kind": "competing_replacements",
                    "entities": sorted(replacements),
                    "history": ident,
                }
            )
    for edge in edges:
        if edge["type"] != "contradicts":
            continue
        left, right = roots.get(edge["source"], set()), roots.get(edge["target"], set())
        if left != right or left == {edge["source"], edge["target"]}:
            blocked.update(left | right)
            conflicts.append(
                {"kind": "contradiction", "entities": sorted({edge["source"], edge["target"]})}
            )
    return roots, blocked, conflicts, cycles


def _label(
    node: dict, parents: dict, roots: dict, blocked: set, verified: bool, enabled: bool
) -> dict:
    ident = node["id"]
    status = "superseded" if parents[ident] else "current"
    if ident in blocked or ident not in roots:
        status = "conflicted"
    if not verified and status != "conflicted":
        status = "unverified"
    if not enabled and status == "current":
        status = "review_required"
    return {**node, "knowledge_status": status, "superseded_by": sorted(parents[ident])}


def classify(nodes: list[dict], edges: list[dict], complete: bool) -> dict:
    enabled = os.getenv("BRAIN_CURRENT_KNOWLEDGE", "true").lower() == "true"
    parents = {node["id"]: set() for node in nodes}
    for edge in edges:
        if edge["type"] == "supersedes":
            parents[edge["target"]].add(edge["source"])
    roots, blocked, conflicts, cycles = _conflicts(parents, edges)
    verified = complete and not cycles
    labelled = [_label(node, parents, roots, blocked, verified, enabled) for node in nodes]
    guidance = [
        node
        for node in labelled
        if node["knowledge_status"] == "current" and node["type"] in {"lesson", "decision"}
    ]
    guidance.sort(key=lambda node: (node["type"] != "decision", node["id"]))
    return {
        "nodes": labelled,
        "guidance": guidance,
        "history": [node for node in labelled if node["knowledge_status"] == "superseded"],
        "conflicts": conflicts,
        "unresolved_knowledge_conflicts": len(conflicts),
        "selection_enabled": enabled,
        "selection_complete": bool(verified),
    }


def select_current(view: GraphReadView, nodes: list[dict], visible, budget: int) -> dict:
    edges, examined, complete = _assertions(view, nodes, visible, budget)
    return {**classify(nodes, edges, complete), "selection_edges_examined": examined}
