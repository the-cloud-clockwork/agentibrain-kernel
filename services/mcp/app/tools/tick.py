"""Brain tick tool — force an on-demand tick via brain-api /tick.

Enqueues a tick request (POST /tick writes a file the tick-drain CronJob picks
up), then optionally polls GET /tick/{job_id} until the tick completes. A
completed tick has drained raw/inbox, folded markers into arcs, recomputed heat,
run the AI synthesis phases, and refreshed the pgvector index — so freshly
ingested content becomes retrievable via kb_search / brain_search_arcs.
"""

from __future__ import annotations

import asyncio
import json
import os

from mcp.server.fastmcp import FastMCP
from tools.common import http_request

BRAIN_API_URL = os.getenv(
    "BRAIN_API_URL",
    os.getenv("OBSIDIAN_READER_URL", "http://agentibrain-brain-api:8080"),
)
BRAIN_API_TOKEN = os.getenv("KB_ROUTER_TOKEN", "")

_TERMINAL = {"completed", "failed"}
_POLL_INTERVAL = 3


def _headers() -> dict:
    h: dict = {}
    if BRAIN_API_TOKEN:
        h["Authorization"] = f"Bearer {BRAIN_API_TOKEN}"
    return h


def register(mcp: FastMCP):

    @mcp.tool()
    async def brain_tick(
        no_ai: bool = False,
        wait: bool = True,
        timeout: int = 45,
        source: str = "mcp-agent",
    ) -> str:
        """Force a brain tick now so new content becomes retrievable.

        Use after brain_ingest, or after writing @lesson/@milestone/@signal/
        @decision markers, when you need the content searchable immediately
        instead of waiting for the scheduled 2h tick. Enqueues a tick request;
        the tick-drain CronJob (every 1 min) runs the 5-phase pipeline and
        refreshes the pgvector index. By default this blocks until the tick
        completes so you know the content is queryable via kb_search /
        brain_search_arcs.

        Args:
            no_ai: Skip the AI phases (summaries / edges / merges). Phase-1-only:
                drains raw/inbox, classifies markers into regions, recomputes
                heat, re-embeds changed arcs. Fast and free. Use when you only
                added markers to existing arcs. Default False = full tick, so
                brand-new content also gets a real summary.
            wait: Poll until the tick reaches completed/failed. Default True.
                False returns the job_id immediately (fire-and-forget).
            timeout: Max seconds to wait when wait=True. Default 45, deliberately
                under the 60s ceiling most MCP clients put on a single tool call —
                a longer wait gets the CALL killed client-side even though the tick
                keeps running server-side, which reads as a failure when it isn't.
                On timeout this returns the job_id with status="pending"; the tick
                still completes, so just re-query or poll GET /tick/{job_id}.
                Raise it only if you know your client's cap is higher.
            source: Label recorded on the request for audit.
        """
        if not BRAIN_API_URL:
            return json.dumps({"error": "BRAIN_API_URL not configured"})

        # Enqueue — /tick takes query params, not a JSON body. POST returns 202,
        # which http_request treats as success and returns the JSON body.
        raw = await http_request(
            "POST",
            f"{BRAIN_API_URL.rstrip('/')}/tick",
            headers=_headers(),
            params={"dry_run": "false", "no_ai": str(no_ai).lower(), "source": source},
            timeout=15,
        )
        try:
            enq = json.loads(raw)
        except json.JSONDecodeError:
            return json.dumps({"error": "invalid enqueue response", "raw": raw[:500]})
        if "error" in enq:
            return json.dumps(enq)

        job_id = enq.get("job_id")
        result: dict = {
            "job_id": job_id,
            "status": enq.get("status", "pending"),
            "no_ai": no_ai,
            "waited": False,
        }

        if not job_id:
            # A healthy enqueue always returns a job_id; its absence is an error,
            # not something to poll (polling GET /tick/None never resolves).
            result["error"] = "enqueue returned no job_id"
            result["note"] = "brain-api /tick did not return a job_id — check the gateway"
            return json.dumps(result, indent=2)

        if not wait:
            result["note"] = f"enqueued job {job_id}; poll GET /tick/{job_id} for status"
            return json.dumps(result, indent=2)

        # Poll until terminal or timeout. No shared poll helper exists — inline.
        # "unknown" (job not found in any queue dir) is treated as terminal after
        # a short grace: request files are never pruned, so a persistent "unknown"
        # means the id is stale/consumed, not in-flight — polling it to the full
        # timeout would burn `timeout` seconds and then falsely report "queued".
        elapsed = 0
        status = result["status"]
        unknown_streak = 0
        while status not in _TERMINAL and elapsed < timeout:
            await asyncio.sleep(_POLL_INTERVAL)
            elapsed += _POLL_INTERVAL
            sraw = await http_request(
                "GET",
                f"{BRAIN_API_URL.rstrip('/')}/tick/{job_id}",
                headers=_headers(),
                timeout=15,
            )
            try:
                status = json.loads(sraw).get("status", status)
            except json.JSONDecodeError:
                continue
            if status == "unknown":
                unknown_streak += 1
                if unknown_streak >= 2:  # ~6s of "not found" — stop, don't hang
                    break
            else:
                unknown_streak = 0

        result["status"] = status
        result["waited"] = True
        result["waited_seconds"] = elapsed
        if status == "completed":
            result["note"] = "tick complete — new content is indexed and retrievable"
        elif status == "failed":
            result["note"] = "tick failed — check tick-drain CronJob logs"
        elif status == "unknown":
            result["note"] = (
                "job not found on the queue — it may have already completed and been "
                "consumed, or the id is stale; re-run your kb_search / brain_search_arcs to check"
            )
        else:
            result["note"] = (
                f"still pending after {elapsed}s — the tick is queued; "
                f"poll GET /tick/{job_id} or just retry your query shortly"
            )
        return json.dumps(result, indent=2)
