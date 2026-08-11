"""Federated Knowledge Base retrieval tools.

kb_search  - fan-out across agentibrain-embeddings (semantic) + brain-api vault (text).
kb_brief   - kb_search + LLM synthesis via any OpenAI-compatible gateway, returns brief + candidate_refs.

These tools are READ-ONLY. The kernel write surface (/feed /signal /marker /tick /ingest)
is exposed by brain-api; for dispatch/build/converse see your upstream artifact-store MCP.
"""

from __future__ import annotations

import asyncio
import json
import os

import aiohttp
from mcp.server.fastmcp import FastMCP


EMBEDDINGS_URL = os.getenv("EMBEDDINGS_URL", "http://agentibrain-embeddings:8080")
EMBEDDINGS_API_KEY = os.environ.get("EMBEDDINGS_API_KEY") or ""
BRAIN_API_URL = os.getenv(
    "BRAIN_API_URL",
    os.getenv("OBSIDIAN_READER_URL", "http://agentibrain-brain-api:8080"),
)
BRAIN_API_TOKEN = (
    os.environ.get("BRAIN_API_TOKEN")
    or os.environ.get("KB_ROUTER_TOKEN")
    or os.environ.get("OBSIDIAN_READER_TOKEN")
    or ""
)
INFERENCE_URL = os.getenv("INFERENCE_URL", "")
INFERENCE_TOKEN_ENV = "INFERENCE_API_KEY"
BRAIN_BRIEF_MODEL = os.getenv("BRAIN_BRIEF_MODEL", "brain-brief")
# Reciprocal Rank Fusion constant. 60 is the value from the original RRF paper
# and the default in every mainstream hybrid-search implementation; it damps
# the gap between adjacent ranks so neither source can dominate on position
# alone.
RRF_K = int(os.getenv("KB_RRF_K", "60"))


async def _search_embeddings(
    query: str, limit: int, min_score: float, producer: str = ""
) -> list[dict]:
    """Call agentibrain-embeddings /search and normalize into common schema."""
    if not EMBEDDINGS_URL or not EMBEDDINGS_API_KEY:
        return []
    headers = {
        "Authorization": f"Bearer {EMBEDDINGS_API_KEY}",
        "Content-Type": "application/json",
    }
    body = {"query": query, "limit": limit, "min_score": min_score}
    # Only sent when asked for — omitting it keeps the default cross-producer
    # search, so existing callers see no change.
    if producer:
        body["producer"] = producer
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{EMBEDDINGS_URL.rstrip('/')}/search",
                json=body,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=20),
            ) as resp:
                if not (200 <= resp.status < 300):
                    return []
                data = await resp.json()
        hits = data.get("results", []) if isinstance(data, dict) else []
        out = []
        for h in hits:
            out.append(
                {
                    "source": "artifact",
                    "ref": h.get("key") or "",
                    "title": (h.get("key") or "").split("/")[-1] or h.get("key", ""),
                    "score": float(h.get("score", 0.0)),
                    "preview": h.get("text_preview") or "",
                    "producer": h.get("producer"),
                    "content_type": h.get("content_type"),
                    "metadata": h.get("metadata") or {},
                }
            )
        return out
    except Exception:
        return []


async def _search_vault(query: str, limit: int) -> list[dict]:
    """Call brain-api /vault/search and normalize into common schema."""
    if not BRAIN_API_URL:
        return []
    headers = {}
    if BRAIN_API_TOKEN:
        headers["Authorization"] = f"Bearer {BRAIN_API_TOKEN}"
    params = {"q": query, "limit": str(limit), "context_lines": "2"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{BRAIN_API_URL.rstrip('/')}/vault/search",
                params=params,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=20),
            ) as resp:
                if not (200 <= resp.status < 300):
                    return []
                data = await resp.json()
        hits = data.get("results", []) if isinstance(data, dict) else []
        out = []
        for h in hits:
            snippets = h.get("snippets") or []
            preview = "\n---\n".join(s.get("snippet", "") for s in snippets[:2]) if snippets else ""
            out.append(
                {
                    "source": "obsidian",
                    "ref": h.get("path") or "",
                    "title": h.get("title") or (h.get("path") or "").split("/")[-1],
                    "score": float(h.get("score", 0)),
                    "preview": preview[:500],
                    "match_count": h.get("match_count", 0),
                    "metadata": {},
                }
            )
        return out
    except Exception:
        return []


async def _inference_chat(system_prompt: str, user_prompt: str, model: str) -> str:
    """Single-shot chat completion via any OpenAI-compatible gateway."""
    if not INFERENCE_URL:
        return "[inference gateway not configured]"
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "max_tokens": 800,
        "temperature": 0.3,
    }
    headers = {"Content-Type": "application/json"}
    token = os.environ.get(INFERENCE_TOKEN_ENV, "")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{INFERENCE_URL.rstrip('/')}/chat/completions",
                json=body,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=200),
            ) as resp:
                data = await resp.json()
                if not (200 <= resp.status < 300):
                    return f"[inference error {resp.status}: {str(data)[:200]}]"
                return data.get("choices", [{}])[0].get("message", {}).get("content", "")
    except Exception as e:
        return f"[inference exception: {e}]"


def register(mcp: FastMCP):
    """Register knowledge-base tools on a FastMCP server."""

    @mcp.tool()
    async def kb_search(
        query: str,
        limit: int = 10,
        include_artifact: bool = True,
        include_obsidian: bool = True,
        min_score: float = 0.0,
        producer: str = "",
    ) -> str:
        """Federated knowledge base search across embeddings (semantic) + Obsidian vault (text).

        Hits from the two sources are fused by Reciprocal Rank Fusion, so a
        strong semantic match is not outranked by a weak keyword one.

        Schema: {source, ref, title, score, preview, metadata, source_rank,
        normalized_score}. `score` is the raw per-source value (cosine
        similarity for artifact hits, a match heuristic for obsidian ones) and
        is not comparable across sources. `source_rank` is the hit's 1-based
        position within its own source. `normalized_score` is the fused rank
        score and is the field results are ordered by.

        Use the `ref` to follow up via brain_get_arc (for arcs) or brain-api /vault/read.

        Args:
            query: Natural language or keyword query.
            limit: Max results per source (default 10).
            include_artifact: Search semantic embeddings (default True).
            include_obsidian: Search Obsidian vault files (default True).
            min_score: Minimum score threshold for semantic hits.
            producer: Restrict semantic hits to one producer, e.g.
                "brain-arc" or "brain-lesson". Empty searches all.
        """
        tasks = []
        if include_artifact:
            tasks.append(_search_embeddings(query, limit, min_score, producer))
        if include_obsidian:
            tasks.append(_search_vault(query, limit))
        if not tasks:
            return json.dumps({"query": query, "count": 0, "results": []})

        batches = await asyncio.gather(*tasks, return_exceptions=False)
        merged: list[dict] = []
        for batch in batches:
            merged.extend(batch)

        # Reciprocal Rank Fusion. The two sources score on scales that have no
        # relationship to each other: the vault's is a match heuristic
        # (capped line count plus flat filename/all-token bonuses, so 30-40 for
        # a file that merely repeats a common word), the embeddings' is cosine
        # similarity in [0,1] (0.33 is a good match). Dividing the vault scores
        # by their own batch max forced the best lexical hit to 1.0 however
        # weak it was, so lexical always beat semantic — a search for the
        # verbatim text of a lesson returned four unrelated documents and not
        # the lesson.
        #
        # RRF uses only each source's internal ordering, so the scales never
        # meet. It also stays correct if the vault's formula changes, which a
        # fixed weight or floor would not, and it handles the cases min-max
        # gets wrong: a source returning a single weak hit no longer becomes a
        # perfect 1.0, and all-equal scores keep a deterministic order instead
        # of collapsing into a tie.
        #
        # Both lists arrive already sorted by their own source, so rank is
        # position — no re-sort needed before fusing.
        artifact_hits = [h for h in merged if h.get("source") == "artifact"]
        obsidian_hits = [h for h in merged if h.get("source") == "obsidian"]
        for hits in (artifact_hits, obsidian_hits):
            for rank, h in enumerate(hits, start=1):
                # Raw per-source `score` is left untouched for debuggability;
                # `source_rank` is the legible companion, since fused values
                # are all ~0.008-0.016 and mean nothing at a glance.
                h["source_rank"] = rank
                h["normalized_score"] = 1.0 / (RRF_K + rank)

        # Rank 1 of each source scores identically, so the top slot is a tie.
        # Break it toward the semantic hit explicitly rather than leaving it to
        # the order tasks happened to be appended in: burying semantic hits
        # under keyword noise is the failure this whole change exists to fix.
        merged.sort(
            key=lambda r: (
                -r.get("normalized_score", 0),
                0 if r.get("source") == "artifact" else 1,
            )
        )
        merged = merged[: limit * 2]

        return json.dumps(
            {
                "query": query,
                "count": len(merged),
                "results": merged,
                "sources_searched": (
                    (["artifact"] if include_artifact else [])
                    + (["obsidian"] if include_obsidian else [])
                ),
            }
        )

    @mcp.tool()
    async def kb_brief(
        query: str,
        limit: int = 8,
        model: str = "",
    ) -> str:
        """Knowledge-base brief synthesizer. Runs kb_search, feeds hits to an LLM, returns 3-5 line synthesis.

        Returns JSON: {query, hits, brief, candidate_refs}.

        Args:
            query: Natural language or keyword query.
            limit: Max hits to feed to the LLM (default 8).
            model: OpenAI-compatible model name to use (default: BRAIN_BRIEF_MODEL env).
        """
        raw = await kb_search(query=query, limit=limit)
        try:
            search_payload = json.loads(raw)
        except json.JSONDecodeError:
            return json.dumps({"error": "kb_search returned invalid JSON"})

        hits = search_payload.get("results", [])[:limit]
        if not hits:
            return json.dumps(
                {
                    "query": query,
                    "hits": [],
                    "brief": "Nothing found in the knowledge base for this query.",
                    "candidate_refs": [],
                }
            )

        lines = []
        for i, h in enumerate(hits):
            src = h.get("source", "?")
            ref = h.get("ref", "")
            title = h.get("title", "")
            preview = (h.get("preview") or "")[:400].replace("\n", " ")
            lines.append(f"[{i + 1}] {src}://{ref} - {title}\n    {preview}")
        digest = "\n\n".join(lines)

        system_prompt = (
            "You are synthesizing what is in the operator's knowledge base about a given topic. "
            "You will be shown search hits from two sources: 'artifact' (semantic store) "
            "and 'obsidian' (vault). Write a 3-5 line brief of what's available, "
            "with specific references to items by their [number]. End by listing 2-4 of the most "
            "relevant refs as a JSON array in this exact format on the final line:\n"
            'CANDIDATE_REFS: ["source://ref", ...]'
        )
        user_prompt = f"Query: {query}\n\nSearch hits:\n{digest}"

        chosen_model = model or BRAIN_BRIEF_MODEL
        brief_content = await _inference_chat(system_prompt, user_prompt, chosen_model)
        if not brief_content:
            return json.dumps(
                {
                    "query": query,
                    "hits": hits,
                    "brief": "[LLM unavailable - returning raw hits]",
                    "candidate_refs": [f"{h.get('source')}://{h.get('ref')}" for h in hits[:3]],
                    "model": chosen_model,
                }
            )

        candidate_refs: list[str] = []
        brief_clean = brief_content
        for line in brief_content.splitlines():
            stripped = line.strip()
            if stripped.startswith("CANDIDATE_REFS:"):
                try:
                    payload = stripped.split("CANDIDATE_REFS:", 1)[1].strip()
                    candidate_refs = json.loads(payload)
                    brief_clean = brief_content.replace(line, "").strip()
                except (json.JSONDecodeError, IndexError):
                    pass
                break
        if not candidate_refs:
            candidate_refs = [f"{h.get('source')}://{h.get('ref')}" for h in hits[:3]]

        return json.dumps(
            {
                "query": query,
                "hits": hits,
                "brief": brief_clean,
                "candidate_refs": candidate_refs,
                "model": chosen_model,
            }
        )
