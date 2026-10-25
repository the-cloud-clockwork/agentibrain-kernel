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


async def _expand_arcs(arcs: list[dict], project: str) -> dict:
    if not BRAIN_API_URL or not BRAIN_API_TOKEN:
        return {"error": "brain API authorization is not configured", "partial": True}
    seeds = [arc["cluster_id"] for arc in arcs[:16] if arc.get("cluster_id")]
    raw = await http_request(
        "POST",
        f"{BRAIN_API_URL.rstrip('/')}/graph/expand",
        headers={"Authorization": f"Bearer {BRAIN_API_TOKEN}"},
        body={
            "seed_ids": [seed if seed.startswith("arc:") else f"arc:{seed}" for seed in seeds],
            "project": project,
        },
        timeout=2,
    )
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"error": "invalid graph response", "partial": True}


async def _search_response(query: str, arcs: list[dict], project: str, expand_graph: bool) -> str:
    response = {"query": query, "count": len(arcs), "arcs": arcs}
    if expand_graph and arcs:
        response["graph"] = await _expand_arcs(arcs, project)
    return json.dumps(response, indent=2)


def register(mcp: FastMCP):
    """Register brain arc tools on a FastMCP server."""

    @mcp.tool()
    async def brain_search_arcs(
        query: str,
        top_k: int = 5,
        min_heat: int = 0,
        min_score: float = 0.0,
        project: str = "",
        expand_graph: bool = False,
    ) -> str:
        """Semantic search over brain arcs using pgvector.

        Searches the content_embeddings table where producer='brain-arc' and
        returns ranked arcs with cosine-similarity scores. Used by /replay skill
        and any agent asking "do what we did for X, but for Y".

        Args:
            query: Natural language description of the arc you want to find.
            top_k: Max number of arcs to return. Default 5.
            min_heat: Filter out arcs whose INDEXED heat is below this. Default 0
                      (no filtering) — results are ranked by similarity instead.
                      The heat carried in the index is a snapshot taken when the
                      arc was last embedded, while heat is recomputed every tick,
                      so the indexed value goes stale and cannot be trusted as a
                      filter: a freshly-ticked arc is still 0, and a promoted arc
                      sitting in frontal-lobe/conscious can read 0 here. Raise it
                      only when you deliberately want the stale-heat cut.
            min_score: Minimum cosine similarity (0.0-1.0). Default 0.0.
            project: Canonical project id, e.g. "github.com/owner/repo". Only
                     arcs grouped under exactly that project are returned; a
                     text mention of the repo is not a match. Empty searches all.
            expand_graph: Include authorized connected history and its evidence paths.
        """
        if not EMBED_KEY:
            return json.dumps({"error": "EMBEDDINGS_API_KEY not set"})

        body = {
            "query": query,
            "producer": "brain-arc",
            "limit": max(top_k * 3, 15),
            "min_score": min_score,
            "project_id": project or None,
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
                    "projects": meta.get("projects", []),
                    "score": r.get("score", 0.0),
                    "preview": (r.get("text_preview") or "")[:200],
                }
            )
            if len(filtered) >= top_k:
                break

        return await _search_response(query, filtered, project, expand_graph)

    @mcp.tool()
    async def brain_read_detail(
        entity_id: str,
        revision: int | None = None,
        section: str = "",
        section_offset: int = 0,
        reference_offset: int = 0,
        content_hash: str = "",
        page_size: int = 16,
    ) -> str:
        """Read canonical full sections, pinned revisions and provenance.

        Pass the returned revision and content hash on every continuation, with
        the same section and returned offsets. Missing detail is an error; this
        tool never substitutes an indexed preview. Entity ids include their type
        prefix, such as arc:cluster or lesson:key. Each section is returned whole.
        """
        if not BRAIN_API_URL:
            return json.dumps({"error": "BRAIN_API_URL not set"})
        params = {
            "entity_id": entity_id,
            "section": section,
            "section_offset": section_offset,
            "reference_offset": reference_offset,
            "content_hash": content_hash,
            "section_limit": page_size,
            "reference_limit": page_size,
        }
        if revision is not None:
            params["revision"] = revision
        headers = {"Authorization": f"Bearer {BRAIN_API_TOKEN}"} if BRAIN_API_TOKEN else {}
        return await http_request(
            "GET",
            f"{BRAIN_API_URL.rstrip(chr(47))}/knowledge/detail",
            headers=headers,
            params=params,
            timeout=15,
        )

    @mcp.tool()
    async def brain_get_arc(cluster_id: str) -> str:
        """Fetch a legacy preview and metadata for a single arc by cluster_id.

        This tool returns an indexed excerpt or at most 2000 vault characters,
        never a guaranteed full source. Use brain_read_detail for complete,
        revision-pinned sections. Preview access remains available during rollback.

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
                            "complete": False,
                            "representation": "preview",
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
                                    "complete": False,
                                    "representation": "preview",
                                    "path": hit["path"],
                                    "content": read_data["content"][:2000],
                                },
                                indent=2,
                            )
            except (json.JSONDecodeError, KeyError):
                pass

        return json.dumps({"error": f"cluster_id not found: {cluster_id}"})
