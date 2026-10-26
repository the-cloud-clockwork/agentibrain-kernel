"""Per-brain retrieval, graph, injection, synthesis and inference policy revisions.

A brain's inference budget is bounded by the deployment ceiling for the shared
backend it reaches through its own key. A revision above that ceiling is
refused; no policy raises the ceiling.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from collections import Counter, deque
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from . import scope
from .retrieval import rank

WINDOW_SECONDS = 60.0
ENVIRONMENT_SYNTHESIS_TOKENS = 2048
DEFAULT_BRAIN = "default"


class Revision(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    ranking: str
    max_items: int = Field(ge=1, le=50)
    edge_budget: int = Field(ge=0, le=rank.EDGE_BUDGET)
    packing: Literal["full", "conservative"]
    max_tokens: int = Field(ge=1, le=12000)
    synthesis_max_tokens: int = Field(ge=1, le=8192)
    inference_concurrency: int = Field(ge=1)
    inference_tokens_per_minute: int = Field(ge=1)


class Document(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    brain_id: str = Field(min_length=1, max_length=128)
    revisions: dict[str, Revision] = Field(min_length=1)


@dataclass(frozen=True)
class Ceiling:
    concurrency: int
    tokens_per_minute: int


@dataclass(frozen=True)
class Active:
    brain_id: str
    revision: str
    source: Literal["file", "environment"]
    policy: Revision
    digest: str

    def describe(self) -> dict:
        return {
            "brain_id": self.brain_id,
            "revision": self.revision,
            "source": self.source,
            "digest": self.digest,
        }


class PolicyRefused(Exception):
    def __init__(self, reason: str, error: str) -> None:
        super().__init__(error)
        self.reason = reason


_refusals: Counter[str] = Counter()
_lock = threading.Lock()


def ceiling() -> Ceiling:
    return Ceiling(
        int(os.getenv("BRAIN_INFERENCE_CEILING_CONCURRENCY") or 0),
        int(os.getenv("BRAIN_INFERENCE_CEILING_TOKENS_PER_MINUTE") or 0),
    )


def _active(revision: str, source: Literal["file", "environment"], policy: Revision) -> Active:
    brain = scope.BRAIN_ID or DEFAULT_BRAIN
    identity = {"brain_id": brain, "revision": revision, "source": source}
    raw = json.dumps({**identity, "policy": policy.model_dump()}, sort_keys=True)
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    return Active(brain, revision, source, policy, digest)


def _environment(revision: str, limit: Ceiling) -> Active:
    packing = os.getenv("BRAIN_CONTEXT_PACKING", "true").lower() != "false"
    policy = Revision.model_construct(
        ranking=os.getenv("BRAIN_RANKING_POLICY", rank.CURRENT),
        max_items=50,
        edge_budget=rank.EDGE_BUDGET,
        packing="full" if packing else "conservative",
        max_tokens=12000,
        synthesis_max_tokens=ENVIRONMENT_SYNTHESIS_TOKENS,
        inference_concurrency=limit.concurrency,
        inference_tokens_per_minute=limit.tokens_per_minute,
    )
    return _active(revision, "environment", policy)


def _refuse(reason: str, error: str) -> PolicyRefused:
    with _lock:
        _refusals[reason] += 1
    return PolicyRefused(reason, error)


def _document(path: str) -> Document:
    try:
        return Document.model_validate_json(Path(path).read_bytes())
    except OSError as exc:
        raise _refuse("policy_unreadable", "the policy document cannot be read") from exc
    except ValidationError as exc:
        raise _refuse("invalid_policy", "the policy document is invalid") from exc


def active() -> Active:
    """The policy this brain serves now; raises PolicyRefused rather than substituting one."""
    revision = os.getenv("BRAIN_POLICY_REVISION", "1")
    limit = ceiling()
    path = os.getenv("BRAIN_POLICY_FILE", "")
    if not path:
        return _environment(revision, limit)
    document = _document(path)
    if document.brain_id != (scope.BRAIN_ID or DEFAULT_BRAIN):
        raise _refuse("brain_audience", "the policy names a brain other than this instance")
    chosen = document.revisions.get(revision)
    if chosen is None:
        raise _refuse("unknown_revision", "BRAIN_POLICY_REVISION names no revision in the policy")
    if chosen.ranking not in rank.POLICIES:
        raise _refuse("unknown_ranking", "the revision names no known ranking policy")
    if limit.concurrency < 1 or limit.tokens_per_minute < 1:
        raise _refuse("ceiling_unset", "the deployment sets no inference ceiling")
    if (
        chosen.inference_concurrency > limit.concurrency
        or chosen.inference_tokens_per_minute > limit.tokens_per_minute
    ):
        raise _refuse(
            "budget_exceeds_ceiling", "the revision's inference budget exceeds the ceiling"
        )
    return _active(revision, "file", chosen)


def digest_or_refusal() -> str:
    try:
        return active().digest
    except PolicyRefused as exc:
        return f"refused:{exc.reason}"


@dataclass(frozen=True)
class Lease:
    brain_id: str
    tokens: int


class InferenceBudget:
    """Concurrency and a rolling token window per brain; 0 leaves a limit unset."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock
        self._lock = threading.Lock()
        self._in_flight: Counter[str] = Counter()
        self._spent: dict[str, deque[tuple[float, int]]] = {}
        self._counts: Counter[tuple[str, str]] = Counter()

    def _window(self, brain: str) -> deque[tuple[float, int]]:
        spent = self._spent.setdefault(brain, deque())
        horizon = self.clock() - WINDOW_SECONDS
        while spent and spent[0][0] <= horizon:
            spent.popleft()
        return spent

    def reserve(self, current: Active) -> Lease | None:
        brain, policy = current.brain_id, current.policy
        tokens = policy.synthesis_max_tokens
        with self._lock:
            spent = self._window(brain)
            concurrency = policy.inference_concurrency
            if concurrency and self._in_flight[brain] >= concurrency:
                self._counts[(brain, "refused_concurrency")] += 1
                return None
            window = policy.inference_tokens_per_minute
            if window and sum(t for _, t in spent) + tokens > window:
                self._counts[(brain, "refused_tokens")] += 1
                return None
            self._in_flight[brain] += 1
            spent.append((self.clock(), tokens))
            self._counts[(brain, "granted")] += 1
            return Lease(brain, tokens)

    def release(self, lease: Lease) -> None:
        with self._lock:
            self._in_flight[lease.brain_id] -= 1

    def usage(self, current: Active) -> dict:
        brain, policy = current.brain_id, current.policy
        with self._lock:
            reserved = sum(t for _, t in self._window(brain))
            return {
                "brain_id": brain,
                "policy_revision": current.revision,
                "in_flight": self._in_flight[brain],
                "concurrency_limit": policy.inference_concurrency,
                "tokens_reserved_window": reserved,
                "tokens_per_minute_limit": policy.inference_tokens_per_minute,
                "granted": self._counts[(brain, "granted")],
                "refused_concurrency": self._counts[(brain, "refused_concurrency")],
                "refused_tokens": self._counts[(brain, "refused_tokens")],
            }


budget = InferenceBudget()


def reserve() -> tuple[Active, Lease] | None:
    try:
        current = active()
    except PolicyRefused:
        return None
    lease = budget.reserve(current)
    return None if lease is None else (current, lease)


def diagnostics() -> dict:
    try:
        current, refusal = active().describe(), None
    except PolicyRefused as exc:
        current, refusal = None, {"reason": exc.reason, "error": str(exc)}
    with _lock:
        refusals = dict(_refusals)
    return {
        "active": current,
        "refusal": refusal,
        "ceiling": asdict(ceiling()),
        "refusals": refusals,
    }


def usage() -> dict | None:
    """brain_inference_budget_usage for the active policy; None while it is refused."""
    try:
        return budget.usage(active())
    except PolicyRefused:
        return None
