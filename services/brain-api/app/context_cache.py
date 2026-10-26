"""Shared task-context results: equivalent concurrent requests share one retrieval.

A key names everything that scopes or shapes a result. A change of grants,
policy or graph knowledge clears every entry before the next lookup is served,
and a load that began before such a change is never stored.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from collections import Counter, OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field

TTL_SECONDS = float(os.getenv("BRAIN_CONTEXT_CACHE_TTL_SECONDS", "30"))
NEGATIVE_TTL_SECONDS = float(os.getenv("BRAIN_CONTEXT_CACHE_NEGATIVE_TTL_SECONDS", "2"))
MAX_ENTRIES = int(os.getenv("BRAIN_CONTEXT_CACHE_ENTRIES", "1024"))
FOLLOWER_WAIT_SECONDS = float(os.getenv("BRAIN_CONTEXT_CACHE_WAIT_SECONDS", "5"))


def enabled() -> bool:
    return os.getenv("BRAIN_CONTEXT_CACHE", "true").lower() != "false"


def digest(value: object) -> str:
    raw = json.dumps(value, sort_keys=True, ensure_ascii=False, default=sorted)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Key:
    brain_id: str
    project_id: str
    task_id: str
    task_revision: int
    lane: str
    purpose: str
    retrieval: str
    policy: str
    grant: str
    knowledge: str


def negative(context: dict) -> bool:
    """An empty or reduced result: nothing ranked, or a source failed or was cut short."""
    return bool(context.get("reduced")) or not (
        context["items"] or context.get("pack", {}).get("omitted")
    )


@dataclass
class _Entry:
    value: dict
    expires: float
    negative: bool


@dataclass
class _Flight:
    generation: int
    done: threading.Event = field(default_factory=threading.Event)
    entry: _Entry | None = None


class ContextCache:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock
        self._lock = threading.Lock()
        self._entries: OrderedDict[Key, _Entry] = OrderedDict()
        self._flights: dict[Key, _Flight] = {}
        self._epoch: object = None
        self._generation = 0
        self._counts: Counter[str] = Counter()

    def _observe(self, epoch: object) -> None:
        if epoch == self._epoch:
            return
        if self._epoch is not None:
            self._counts["invalidations"] += 1
        self._epoch = epoch
        self._entries.clear()
        self._flights.clear()
        self._generation += 1

    def observe(self, epoch: object) -> None:
        with self._lock:
            self._observe(epoch)

    def _meta(self, state: str, entry: _Entry) -> dict:
        ttl = NEGATIVE_TTL_SECONDS if entry.negative else TTL_SECONDS
        return {"state": state, "negative": entry.negative, "ttl_seconds": ttl}

    def _join(self, key: Key, epoch: object) -> tuple[_Entry | None, _Flight, bool]:
        with self._lock:
            self._counts["requests"] += 1
            self._observe(epoch)
            entry = self._entries.get(key)
            if entry is not None and entry.expires > self.clock():
                self._entries.move_to_end(key)
                self._counts["hits"] += 1
                return entry, _Flight(self._generation), False
            self._entries.pop(key, None)
            flight = self._flights.get(key)
            if flight is not None:
                self._counts["coalesced"] += 1
                return None, flight, False
            flight = self._flights[key] = _Flight(self._generation)
            self._counts["misses"] += 1
            return None, flight, True

    def _store(self, key: Key, flight: _Flight, entry: _Entry, epoch: object) -> None:
        with self._lock:
            if self._flights.get(key) is flight:
                del self._flights[key]
            self._observe(epoch)
            if flight.generation != self._generation:
                return
            self._entries[key] = entry
            while len(self._entries) > MAX_ENTRIES:
                self._entries.popitem(last=False)

    def _abandon(self, key: Key, flight: _Flight) -> None:
        with self._lock:
            if self._flights.get(key) is flight:
                del self._flights[key]
        flight.done.set()

    def get(
        self, key: Key, epoch: Callable[[], object], load: Callable[[], dict]
    ) -> tuple[dict, dict]:
        """Return the context for key and how it was served: hit, coalesced, miss or bypass."""
        cached, flight, leader = self._join(key, epoch())
        if cached is not None:
            return cached.value, self._meta("hit", cached)
        if not leader:
            flight.done.wait(FOLLOWER_WAIT_SECONDS)
            with self._lock:
                current = flight.generation == self._generation
            if flight.entry is not None and current:
                return flight.entry.value, self._meta("coalesced", flight.entry)
            value = load()
            return value, self._meta("bypass", _Entry(value, 0.0, negative(value)))
        try:
            value = load()
        except BaseException:
            self._abandon(key, flight)
            raise
        is_negative = negative(value)
        ttl = NEGATIVE_TTL_SECONDS if is_negative else TTL_SECONDS
        entry = _Entry(value, self.clock() + ttl, is_negative)
        self._store(key, flight, entry, epoch())
        flight.entry = entry
        flight.done.set()
        return value, self._meta("miss", entry)

    def stats(self) -> dict:
        with self._lock:
            counts = dict(self._counts)
            entries = len(self._entries)
        requests = counts.get("requests", 0)
        shared = counts.get("hits", 0) + counts.get("coalesced", 0)
        return {
            "context_cache_requests": requests,
            "context_cache_hits": counts.get("hits", 0),
            "context_cache_misses": counts.get("misses", 0),
            "context_coalesced_requests": counts.get("coalesced", 0),
            "context_cache_hit_ratio": shared / requests if requests else 0.0,
            "context_cache_invalidations": counts.get("invalidations", 0),
            "context_cache_entries": entries,
        }
