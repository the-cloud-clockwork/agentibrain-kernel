"""Python SDK — thin HTTP client for the kernel API.

Downstream consumers (agentihooks, CLI tools, test harnesses) import this instead
of hand-rolling ``httpx`` calls. Keeps the HTTP contract in one place.

Phase 5 / Phase 7 implementation. Stubs for now.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx


@dataclass
class BrainClient:
    """Thin HTTP client for the kernel API."""

    base_url: str
    token: str
    timeout: float = 30.0

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    def health(self) -> dict:
        """GET /health."""
        with httpx.Client(base_url=self.base_url, timeout=self.timeout) as c:
            r = c.get("/health", headers=self._headers())
            r.raise_for_status()
            return r.json()

    def ingest(self, payload: dict) -> dict:
        """POST /ingest — Phase 5."""
        raise NotImplementedError

    def search(self, query: str, **kwargs: object) -> dict:
        """POST /search — Phase 5."""
        raise NotImplementedError

    def brief(self, query: str, **kwargs: object) -> dict:
        """POST /brief — Phase 5."""
        raise NotImplementedError

    def feed(self) -> dict:
        """GET /feed — Phase 7 (replaces brain_adapter's vault reads)."""
        raise NotImplementedError

    def marker(self, marker_type: str, content: str, **kwargs: object) -> dict:
        """POST /marker — Phase 7 (replaces brain_writer_hook direct vault writes)."""
        raise NotImplementedError
