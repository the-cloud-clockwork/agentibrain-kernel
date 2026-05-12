"""Brain ingest tool — write content directly to brain-api /ingest."""

from __future__ import annotations

import json
import os

from mcp.server.fastmcp import FastMCP

import aiohttp


BRAIN_API_URL = os.getenv(
    "BRAIN_API_URL",
    os.getenv("OBSIDIAN_READER_URL", "http://agentibrain-brain-api:8080"),
)
BRAIN_API_TOKEN = os.getenv("KB_ROUTER_TOKEN", "")


def _headers() -> dict:
    h: dict = {}
    if BRAIN_API_TOKEN:
        h["Authorization"] = f"Bearer {BRAIN_API_TOKEN}"
    return h


def register(mcp: FastMCP):

    @mcp.tool()
    async def brain_ingest(
        content: str,
        title: str = "",
        producer: str = "agent",
        chunk_size: int = 200_000,
    ) -> str:
        """Write text directly to the brain vault via brain-api /ingest.

        Content lands in raw/inbox/ and is processed by the next brain tick.
        Large content is chunked automatically.

        Args:
            content: Text to ingest. Can be a full book — chunked at chunk_size.
            title: Optional label prepended to each chunk for context.
            producer: Tag for the source agent (default "agent").
            chunk_size: Max chars per chunk (default 200_000 ≈ 50k tokens).
        """
        if not BRAIN_API_URL:
            return json.dumps({"error": "BRAIN_API_URL not configured"})
        if not content or not content.strip():
            return json.dumps({"error": "content is empty"})

        chunks = [content[i:i + chunk_size] for i in range(0, len(content), chunk_size)]
        label = title or "document"
        results: list[dict] = []

        for i, chunk in enumerate(chunks):
            if len(chunks) > 1:
                prefix = f"[Part {i + 1}/{len(chunks)} of {label}]\n\n"
            elif title:
                prefix = f"[{label}]\n\n"
            else:
                prefix = ""

            try:
                form = aiohttp.FormData()
                form.add_field("message", prefix + chunk)
                form.add_field("producer", producer)
                async with aiohttp.ClientSession() as session:
                    async with session.post(
                        f"{BRAIN_API_URL.rstrip('/')}/ingest",
                        data=form,
                        headers=_headers(),
                        timeout=aiohttp.ClientTimeout(total=180),
                    ) as resp:
                        body = await resp.json()
                        ok = 200 <= resp.status < 300
                        results.append({"chunk": i + 1, "ok": ok, "status": resp.status, "detail": body})
            except Exception as exc:
                results.append({"chunk": i + 1, "ok": False, "error": str(exc)})

        ok_count = sum(1 for r in results if r.get("ok"))
        return json.dumps({
            "chunks_sent": ok_count,
            "total_chunks": len(chunks),
            "title": title or None,
            "producer": producer,
            "results": results,
        }, indent=2)
