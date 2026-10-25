"""Evidence-aware normalization of arc edge assertions (SV2-GRF-02).

Every frontmatter ``edges:`` entry and inline ``@edge`` marker is one
assertion. Assertions of the same relation merge and keep all their evidence;
an inverse name is stored as its canonical edge and symmetric relations order
their endpoints. Self loops, unsupported or malformed markers and pairs whose
relations contradict each other go to quarantine with every assertion
involved: a conflict is never settled by ranking edge types.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

import markers

SUPPORTED = frozenset({"parent", "child", "sibling", "related", "unblocks", "supersedes"})
INVERSE = {"child": "parent"}
SYMMETRIC = frozenset({"sibling", "related"})
DIRECTED = frozenset({"parent", "unblocks", "supersedes"})
EXCLUSIVE = (frozenset({"parent", "sibling"}),)
_FM_EDGES_RE = re.compile(r"^edges:\s*(.*)$")


@dataclass(frozen=True)
class Assertion:
    source: str
    type: str
    target: str
    evidence: str
    origin: str


def norm_id(value: str) -> str:
    return markers.canonical_arc_id(value.strip().strip("`").strip())


def arc_id(text: str, path: Path) -> str:
    fm, _ = markers.parse_frontmatter(text)
    cid = fm.get("cluster_id")
    return norm_id(cid) if isinstance(cid, str) and cid.strip() else norm_id(path.name)


def arc_project(text: str) -> str:
    project = markers.parse_frontmatter(text)[0].get("project")
    return project if isinstance(project, str) and project else "unknown"


def _frontmatter_assertions(text: str, source: str, ref: str) -> list[Assertion]:
    if not text.startswith(markers.FM_DELIM):
        return []
    found = []
    head = text.split(markers.FM_DELIM, 2)[1] if text.count(markers.FM_DELIM) >= 2 else ""
    for offset, line in enumerate(head.splitlines()):
        match = _FM_EDGES_RE.match(line.strip())
        if not match:
            continue
        evidence = f"{ref}#L{offset + 1}"
        try:
            items = json.loads(match.group(1) or "[]")
        except ValueError:
            items = None
        if not isinstance(items, list):
            found.append(Assertion(source, "", "", evidence, "frontmatter"))
            continue
        for item in items:
            pairs = list(item.items()) if isinstance(item, dict) and len(item) == 1 else [("", "")]
            kind, target = pairs[0]
            target = target if isinstance(target, str) else ""
            found.append(Assertion(source, str(kind), norm_id(target), evidence, "frontmatter"))
    return found


def file_assertions(path: Path, ref: str) -> list[Assertion]:
    text = path.read_text()
    source = arc_id(text, path)
    found = _frontmatter_assertions(text, source, ref)
    for marker in markers.find_markers(text, "edge"):
        found.append(
            Assertion(
                source,
                marker.attr("type"),
                norm_id(marker.attr("target")),
                f"{ref}#L{marker.line_start + 1}",
                "inline",
            )
        )
    return found


def _invalid(a: Assertion) -> str | None:
    if not a.type or not a.target or not a.source:
        return "malformed"
    if a.type not in SUPPORTED:
        return "unsupported_type"
    if a.source == a.target:
        return "self_loop"
    return None


def canonical(a: Assertion) -> tuple[str, str, str]:
    kind = INVERSE.get(a.type, a.type)
    source, target = (a.target, a.source) if a.type in INVERSE else (a.source, a.target)
    if kind in SYMMETRIC:
        source, target = sorted((source, target))
    return source, kind, target


def relation_id(key: tuple[str, str, str]) -> str:
    return "rel-" + hashlib.sha256("\0".join(key).encode()).hexdigest()[:32]


def _quarantine_id(reasons: list[str], keys: list[tuple[str, str, str]]) -> str:
    body = json.dumps([reasons, sorted(keys)])
    return "q-" + hashlib.sha256(body.encode()).hexdigest()[:32]


def _pair_conflicts(keys: set[tuple[str, str, str]]) -> dict[tuple[str, str, str], set[str]]:
    found: dict[tuple[str, str, str], set[str]] = {}
    for source, kind, target in keys:
        if kind in DIRECTED and (target, kind, source) in keys:
            found.setdefault((source, kind, target), set()).add("contradictory_direction")
    kinds = {k[1] for k in keys}
    for exclusive in EXCLUSIVE:
        if len(kinds & exclusive) > 1:
            for key in keys:
                if key[1] in exclusive:
                    found.setdefault(key, set()).add("conflicting_types")
    return found


def _group(assertions: list[Assertion]) -> tuple[dict, dict]:
    invalid: dict[tuple, list[int]] = {}
    valid: dict[tuple[str, str, str], list[int]] = {}
    for i, a in enumerate(assertions):
        reason = _invalid(a)
        if reason:
            invalid.setdefault((reason, a.source, a.type, a.target), []).append(i)
        else:
            valid.setdefault(canonical(a), []).append(i)
    return invalid, valid


def _conflict_entries(valid: dict) -> list[tuple[list[str], list[tuple[str, str, str]]]]:
    pairs: dict[frozenset[str], set[tuple[str, str, str]]] = {}
    for key in valid:
        pairs.setdefault(frozenset((key[0], key[2])), set()).add(key)
    entries = []
    for keys in pairs.values():
        conflicts = _pair_conflicts(keys)
        if conflicts:
            reasons = sorted(set().union(*conflicts.values()))
            entries.append((reasons, sorted(conflicts)))
    return entries


def _accepted_review(review: dict | None, keys: list[tuple[str, str, str]]):
    if not isinstance(review, dict) or review.get("decision") != "accept":
        return None
    relation = review.get("relation")
    chosen = tuple(relation) if isinstance(relation, list) else None
    return chosen if chosen in keys else None


def _quarantine(groups: list, assertions: list[Assertion], reviews: dict):
    quarantine, decisions, reviewed = [], {}, {}
    for reasons, keys, members in groups:
        qid = _quarantine_id(reasons, keys)
        quarantine.append(
            {
                "id": qid,
                "reasons": reasons,
                "relations": [
                    {"relation": list(k), "evidence": [assertions[i].evidence for i in members[k]]}
                    for k in keys
                ],
                "review": reviews.get(qid),
                "present": True,
            }
        )
        for k in keys:
            for i in members[k]:
                decisions[i] = {"decision": "quarantined", "reasons": reasons, "quarantine": qid}
        chosen = _accepted_review(reviews.get(qid), keys)
        if chosen:
            reviewed[chosen] = qid
    return quarantine, decisions, reviewed


def _relations(
    accepted: dict, reviewed: dict, assertions: list[Assertion], projects: dict, decisions: dict
) -> list[dict]:
    relations = []
    for key, members in accepted.items():
        evidence = [assertions[i].evidence for i in members]
        if key in reviewed:
            evidence.append(f"review:{reviewed[key]}")
        else:
            for n, i in enumerate(members):
                decision = "merged_duplicate" if n else "accepted"
                decisions[i] = {"decision": decision, "relation": relation_id(key)}
        relations.append(
            {
                "id": relation_id(key),
                "source": key[0],
                "type": key[1],
                "target": key[2],
                "status": "accepted",
                "confidence": "extracted",
                "evidence": evidence,
                "scope": {
                    "source": projects.get(key[0], "unknown"),
                    "target": projects.get(key[2], "unknown"),
                },
            }
        )
    return sorted(relations, key=lambda r: r["id"])


def normalize(
    assertions: list[Assertion], reviews: dict | None = None, projects: dict | None = None
) -> dict:
    reviews = reviews or {}
    invalid, valid = _group(assertions)
    groups = [
        ([reason], [(s, k, t)], {(s, k, t): members})
        for (reason, s, k, t), members in sorted(invalid.items())
    ]
    conflicts = _conflict_entries(valid)
    groups += [(reasons, keys, valid) for reasons, keys in conflicts]
    quarantine, decisions, reviewed = _quarantine(groups, assertions, reviews)
    conflicted = {key for _, keys in conflicts for key in keys}
    accepted = {k: m for k, m in valid.items() if k not in conflicted or k in reviewed}
    relations = _relations(accepted, reviewed, assertions, projects or {}, decisions)

    present = {q["id"] for q in quarantine}
    quarantine += [
        {"id": qid, "reasons": [], "relations": [], "review": review, "present": False}
        for qid, review in sorted(reviews.items())
        if qid not in present
    ]
    audit = [
        {
            "evidence": a.evidence,
            "origin": a.origin,
            "asserted": [a.source, a.type, a.target],
            **decisions[i],
        }
        for i, a in enumerate(assertions)
    ]
    return {
        "relations": relations,
        "quarantine": sorted(quarantine, key=lambda q: q["id"]),
        "audit": audit,
    }
