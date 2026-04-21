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
