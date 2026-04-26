"""Brain arc retrieval tools — semantic search over cluster files via agentibrain-embeddings."""

from __future__ import annotations

import json
import os

from mcp.server.fastmcp import FastMCP

from tools.common import http_request


EMBED_URL = os.getenv("EMBEDDINGS_URL", "http://agentibrain-embeddings:8080")
EMBED_KEY = os.getenv("EMBEDDINGS_API_KEY", "")


def _headers() -> dict:
    return {"Authorization": f"Bearer {EMBED_KEY}"}


def register(mcp: FastMCP):
    """Register brain arc tools on a FastMCP server."""

    @mcp.tool()
    async def brain_search_arcs(
        query: str,
        top_k: int = 5,
        min_heat: int = 2,
        min_score: float = 0.0,
    ) -> str:
        """Semantic search over brain arcs using pgvector.

        Searches the content_embeddings table where producer='brain-arc' and
        returns ranked arcs with cosine-similarity scores. Used by /replay skill
        and any agent asking "do what we did for X, but for Y".

        Args:
            query: Natural language description of the arc you want to find.
            top_k: Max number of arcs to return. Default 5.
            min_heat: Filter out arcs below this heat score. Default 2 (drops
                      noise). Pass 0 to search all arcs including graduated.
            min_score: Minimum cosine similarity (0.0-1.0). Default 0.0.
        """
        if not EMBED_KEY:
            return json.dumps({"error": "EMBEDDINGS_API_KEY not set"})

        body = {
            "query": query,
            "producer": "brain-arc",
            "limit": max(top_k * 3, 15),
            "min_score": min_score,
        }
        raw = await http_request(
            "POST",
            f"{EMBED_URL.rstrip('/')}/search",
            headers=_headers(),
            body=body,
            timeout=15,
        )
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return json.dumps({"error": "invalid response", "raw": raw[:500]})

        if "error" in data:
            return json.dumps(data)

        results = data.get("results") or []
        filtered = []
        for r in results:
            meta = r.get("metadata") or {}
            try:
                heat = int(meta.get("heat", 0))
            except (TypeError, ValueError):
                heat = 0
            if heat < min_heat:
                continue
            filtered.append(
                {
                    "cluster_id": r.get("key"),
                    "title": meta.get("title", "") or r.get("key"),
                    "region": meta.get("region", ""),
                    "heat": heat,
                    "status": meta.get("status", ""),
                    "path": meta.get("path", ""),
                    "score": r.get("score", 0.0),
                    "preview": (r.get("text_preview") or "")[:200],
                }
            )
            if len(filtered) >= top_k:
                break

        return json.dumps(
            {
                "query": query,
                "count": len(filtered),
                "arcs": filtered,
            },
            indent=2,
        )

    @mcp.tool()
    async def brain_get_arc(cluster_id: str) -> str:
        """Fetch the full text preview and metadata for a single arc by cluster_id.

        Useful as a follow-up to brain_search_arcs when you need more than the
        short preview. Returns the first chunk's text_preview (up to ~2000 chars).

        Args:
            cluster_id: The arc's cluster_id (the `key` field from brain_search_arcs).
        """
        if not EMBED_KEY:
            return json.dumps({"error": "EMBEDDINGS_API_KEY not set"})

        body = {
            "query": cluster_id,
            "producer": "brain-arc",
            "limit": 50,
            "min_score": 0.0,
        }
        raw = await http_request(
            "POST",
            f"{EMBED_URL.rstrip('/')}/search",
            headers=_headers(),
            body=body,
            timeout=15,
        )
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return json.dumps({"error": "invalid response", "raw": raw[:500]})

        for r in data.get("results") or []:
            if r.get("key") == cluster_id:
                return json.dumps(
                    {
                        "cluster_id": cluster_id,
                        "metadata": r.get("metadata") or {},
                        "text_preview": r.get("text_preview", ""),
                    },
                    indent=2,
                )
        return json.dumps({"error": f"cluster_id not found: {cluster_id}"})
