"""Python SDK — thin HTTP client for the kernel API.

Downstream consumers (agentihooks, CLI tools, test harnesses) import this instead
of hand-rolling ``httpx`` calls. Keeps the HTTP contract in one place.

Phase 5 / Phase 7 implementation. Stubs for now.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass

import httpx

from agentibrain.config import BRAIN_ID_PATTERN, DEFAULT_BRAIN_ID

brain_instance_binding_failures: Counter[tuple[str, str]] = Counter()


def _label(observed: object) -> str:
    if observed is None:
        return "None"
    if isinstance(observed, str) and re.fullmatch(BRAIN_ID_PATTERN, observed):
        return observed
    return "invalid"


class BrainBindingError(RuntimeError):
    """The endpoint is not the brain the client was configured for."""


@dataclass
class BrainClient:
    """Thin HTTP client for the kernel API."""

    base_url: str
    token: str
    timeout: float = 30.0
    expected_brain_id: str = DEFAULT_BRAIN_ID
    transport: httpx.BaseTransport | None = None

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    def _client(self) -> httpx.Client:
        return httpx.Client(base_url=self.base_url, timeout=self.timeout, transport=self.transport)

    def verify_instance(self) -> dict:
        """GET /instance without credentials; raise unless it names the expected brain."""
        with self._client() as c:
            r = c.get("/instance")
        try:
            body = r.json() if r.status_code == 200 else {}
        except ValueError:
            body = {}
        observed = body.get("brain_id") if isinstance(body, dict) else None
        if observed != self.expected_brain_id:
            label = _label(observed)
            brain_instance_binding_failures[(self.expected_brain_id, label)] += 1
            raise BrainBindingError(
                f"expected brain {self.expected_brain_id}, {self.base_url} answered {label}"
            )
        return body

    def health(self) -> dict:
        """GET /health."""
        self.verify_instance()
        with self._client() as c:
            r = c.get("/health", headers=self._headers())
            r.raise_for_status()
            return r.json()

    def ingest(self, payload: dict) -> dict:
        """POST /ingest — wiring pending."""
        raise NotImplementedError("BrainClient.ingest not wired yet — call POST /ingest directly.")

    def search(self, query: str, **kwargs: object) -> dict:
        """POST /search — wiring pending."""
        raise NotImplementedError("BrainClient.search not wired yet — call POST /search directly.")

    def brief(self, query: str, **kwargs: object) -> dict:
        """POST /brief — wiring pending."""
        raise NotImplementedError("BrainClient.brief not wired yet — call POST /brief directly.")

    def feed(self) -> dict:
        """GET /feed — Phase 7 (replaces brain_adapter's vault reads)."""
        raise NotImplementedError(
            "BrainClient.feed requires the Phase 7 kernel /feed endpoint (not shipped yet)."
        )

    def marker(self, marker_type: str, content: str, **kwargs: object) -> dict:
        """POST /marker — Phase 7 (replaces brain_writer_hook direct vault writes)."""
        raise NotImplementedError(
            "BrainClient.marker requires the Phase 7 kernel /marker endpoint (not shipped yet)."
        )
