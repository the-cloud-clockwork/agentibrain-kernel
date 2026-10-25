"""Pack ranked task context into an explicit token budget.

An item is packed whole or omitted with an expandable reference; item text is
never cut. Conflicted items are never merged away and are always named.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import string

SHARES = {"decision": 0.4, "lesson": 0.4, "arc": 0.2}
REFERENCE_FIELDS = ("id", "type", "knowledge_status", "revision", "authority")
_CLAIM = re.compile(r"[\s" + re.escape(string.punctuation) + r"]+")


def tokens(value: object) -> int:
    raw = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return math.ceil(len(raw) / 4)


def category_budgets(max_tokens: int) -> dict[str, int]:
    return {kind: math.floor(max_tokens * share) for kind, share in SHARES.items()}


def knowledge_revision(items: list[dict]) -> str:
    basis = sorted(
        json.dumps(
            [item["id"], item.get("revision"), item["knowledge_status"], item["title"]],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        for item in items
    )
    raw = "\n".join(basis)
    return "kr-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _reference(item: dict) -> dict:
    return {"entity_id": item["id"], "revision": item.get("revision")}


def _claim(item: dict) -> tuple[str, str]:
    claim = _CLAIM.sub(" ", item["title"].casefold()).strip()
    return item["type"], claim or item["title"]


def _deduplicate(items: list[dict]) -> tuple[list[dict], list[dict]]:
    kept: dict[tuple[str, str], dict] = {}
    unique, duplicates = [], []
    for item in items:
        key = _claim(item)
        first = kept.get(key)
        if item["knowledge_status"] == "conflicted" or first is None:
            if item["knowledge_status"] != "conflicted":
                kept[key] = item = {**item, "supporting_source_ids": [item["id"]]}
            unique.append(item)
            continue
        first["supporting_source_ids"].append(item["id"])
        duplicates.append({"item": item, "reason": "duplicate", "duplicate_of": first["id"]})
    return unique, duplicates


def _omission(item: dict, reason: str, cost: int, **extra) -> dict:
    return {
        "id": item["id"],
        "type": item["type"],
        "reason": reason,
        "tokens": cost,
        "reference": _reference(item),
        **extra,
    }


def _entry(item: dict, conservative: bool) -> dict:
    if not conservative:
        return {**item, "complete": True, "reference": _reference(item)}
    return {
        **{key: item.get(key) for key in REFERENCE_FIELDS},
        "complete": False,
        "reference": _reference(item),
    }


def _units(unique: list[dict], conservative: bool) -> list[list[tuple]]:
    units: list[list[tuple]] = []
    conflicted: list[tuple] = []
    for item in unique:
        entry = _entry(item, conservative)
        member = (item, entry, tokens(entry))
        if item["knowledge_status"] != "conflicted":
            units.append([member])
            continue
        if not conflicted:
            units.append(conflicted)
        conflicted.append(member)
    return units


def _select(unique: list[dict], max_tokens: int, conservative: bool) -> tuple:
    caps = category_budgets(max_tokens)
    spent = dict.fromkeys(caps, 0)
    used, packed, deferred = 0, [], []
    for unit in _units(unique, conservative):
        need = dict.fromkeys(caps, 0)
        for item, _, cost in unit:
            need[item["type"]] += cost
        total = sum(need.values())
        if used + total <= max_tokens and all(spent[k] + need[k] <= caps[k] for k in caps):
            for kind in caps:
                spent[kind] += need[kind]
            used += total
            packed.extend(entry for _item, entry, _cost in unit)
        else:
            deferred.append((unit, need, total))
    omitted = []
    for unit, need, total in deferred:
        if used + total <= max_tokens:
            for kind in caps:
                spent[kind] += need[kind]
            used += total
            packed.extend(entry for _item, entry, _cost in unit)
        else:
            omitted.extend(_omission(item, "over_budget", cost) for item, _, cost in unit)
    return caps, spent, used, packed, omitted


def pack(context: dict, max_tokens: int, conservative: bool = False) -> dict:
    ranked = context["items"]
    unique, duplicates = _deduplicate(ranked)
    caps, spent, used, packed, omitted = _select(unique, max_tokens, conservative)
    order = {item["id"]: index for index, item in enumerate(ranked)}
    packed.sort(key=lambda entry: order[entry["id"]])
    for duplicate in duplicates:
        item = duplicate["item"]
        omitted.append(_omission(item, "duplicate", 0, duplicate_of=duplicate["duplicate_of"]))
    omitted.sort(key=lambda entry: order[entry["id"]])
    budget_omitted = sum(entry["reason"] == "over_budget" for entry in omitted)
    return {
        **context,
        "items": packed,
        "pack": {
            "max_tokens": max_tokens,
            "category_tokens": caps,
            "category_spent": spent,
            "context_pack_tokens": used,
            "omitted_items": len(omitted),
            "omitted": omitted,
            "truncated": budget_omitted > 0,
            "unresolved_conflicts": sorted(
                item["id"] for item in ranked if item["knowledge_status"] == "conflicted"
            ),
            "knowledge_revision": knowledge_revision(ranked),
            "degraded": "packing_disabled" if conservative else None,
            "authority": "recalled_evidence",
        },
    }
