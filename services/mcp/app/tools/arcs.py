"""Brain arc retrieval tools — semantic search over cluster files via agentibrain-embeddings."""

from __future__ import annotations

import json
import os

from mcp.server.fastmcp import FastMCP

from tools.common import http_request


EMBED_URL = os.getenv("EMBEDDINGS_URL", "http://agentibrain-embeddings:8080")
EMBED_KEY = os.getenv("EMBEDDINGS_API_KEY", "")
BRAIN_API_URL = os.getenv(
    "BRAIN_API_URL",
    os.getenv("OBSIDIAN_READER_URL", "http://agentibrain-brain-api:8080"),
)
BRAIN_API_TOKEN = os.getenv("KB_ROUTER_TOKEN", "")


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
        short preview. Tries direct DB lookup first, falls back to vault file read.

        Args:
            cluster_id: The arc's cluster_id (the `key` field from brain_search_arcs).
        """
        # Try direct key lookup in pgvector (fast, no embedding needed)
        if EMBED_KEY:
            raw = await http_request(
                "GET",
                f"{EMBED_URL.rstrip('/')}/by-key/{cluster_id}",
                headers=_headers(),
                timeout=10,
            )
            try:
                data = json.loads(raw)
                if "chunks" in data and data["chunks"]:
                    chunk = data["chunks"][0]
                    return json.dumps(
                        {
                            "cluster_id": cluster_id,
                            "source": "embeddings",
                            "metadata": chunk.get("metadata") or {},
                            "text_preview": chunk.get("text_preview", ""),
                        },
                        indent=2,
                    )
            except json.JSONDecodeError:
                pass

        # Fallback: search vault for arc file by cluster_id filename
        if BRAIN_API_URL:
            headers = {}
            if BRAIN_API_TOKEN:
                headers["Authorization"] = f"Bearer {BRAIN_API_TOKEN}"
            raw = await http_request(
                "GET",
                f"{BRAIN_API_URL.rstrip('/')}/vault/search",
                headers=headers,
                params={"q": cluster_id, "limit": "3"},
                timeout=10,
            )
            try:
                data = json.loads(raw)
                for hit in data.get("results", []):
                    if cluster_id in hit.get("path", ""):
                        read_raw = await http_request(
                            "GET",
                            f"{BRAIN_API_URL.rstrip('/')}/vault/read",
                            headers=headers,
                            params={"path": hit["path"]},
                            timeout=10,
                        )
                        read_data = json.loads(read_raw)
                        if read_data.get("content"):
                            return json.dumps(
                                {
                                    "cluster_id": cluster_id,
                                    "source": "vault",
                                    "path": hit["path"],
                                    "content": read_data["content"][:2000],
                                },
                                indent=2,
                            )
            except (json.JSONDecodeError, KeyError):
                pass

        return json.dumps({"error": f"cluster_id not found: {cluster_id}"})
