"""Direct vault access — read a known file, list a directory, see the feed.

Search answers "where might this be". These answer "show me this", which is a
different question and the one an agent asks once it already knows the path.

Without them, an agent that correctly wanted `brain-feed/lessons.md` had no tool
that could open it. Observed consequence: it fell back to guessing filesystem
paths on the host — `~/.agentibrain/vault/brain-feed/` — which is not where the
vault lives, found nothing, and kept guessing. The vault is mounted inside
brain-api and may not exist on the caller's machine at all, so no amount of
local searching can succeed; the only correct route is over the API.

brain-api has served these endpoints all along. They were simply never exposed,
and a missing capability is a missing tool, not an invitation to shell out.
"""

from __future__ import annotations

import os

from mcp.server.fastmcp import FastMCP

from tools.common import http_request


BRAIN_API_URL = os.getenv(
    "BRAIN_API_URL",
    os.getenv("OBSIDIAN_READER_URL", "http://agentibrain-brain-api:8080"),
)
BRAIN_API_TOKEN = os.getenv("KB_ROUTER_TOKEN", "")


def _headers() -> dict:
    return {"Authorization": f"Bearer {BRAIN_API_TOKEN}"} if BRAIN_API_TOKEN else {}


def register(mcp: FastMCP):
    """Register direct vault-access tools on a FastMCP server."""

    @mcp.tool()
    async def brain_feed() -> str:
        """What this session's context is built from, right now.

        Returns the live feed: hot arcs, inject blocks, and the remaining
        entries — recent lessons, active signals, operator intent. This is the
        authoritative answer to "what are my latest lessons" and "what is
        currently alarming": it reads the file the tick writes, so it needs no
        search, no embedding, and no guess about phrasing.

        Prefer this over kb_search when the question is "what is current".
        Search is for "what do we know about X".

        An empty lessons entry here means no lessons have been written to THIS
        vault — not that a query failed. That distinction is the whole reason
        this tool exists.
        """
        return await http_request(
            "GET", f"{BRAIN_API_URL.rstrip('/')}/feed", headers=_headers(), timeout=15
        )

    @mcp.tool()
    async def vault_read(path: str, max_bytes: int = 100_000) -> str:
        """Read one vault file by its path, relative to the vault root.

        Example paths: `brain-feed/lessons.md`, `brain-feed/signals.md`,
        `left/reference/lessons-2026-08-12.md`, or the `path` from any
        kb_search / brain_search_arcs hit.

        The vault lives inside brain-api, typically on an NFS mount or a docker
        volume, and generally does not exist on the machine running this agent.
        Reading it over this tool is the only route that works; a local `find`
        or `cat` is searching a filesystem the vault was never on.
        """
        return await http_request(
            "GET",
            f"{BRAIN_API_URL.rstrip('/')}/vault/read",
            headers=_headers(),
            params={"path": path, "max_bytes": str(max_bytes)},
            timeout=15,
        )

    @mcp.tool()
    async def vault_list(prefix: str = "", limit: int = 200) -> str:
        """List vault files under a directory prefix.

        Use it to find the exact path before `vault_read`, or to answer "what
        is actually in here" — `left/reference` for lesson logs, `amygdala` for
        raw signals, `clusters` for un-graduated arcs, `brain-feed` for what a
        session is fed. An empty prefix lists from the vault root.
        """
        return await http_request(
            "GET",
            f"{BRAIN_API_URL.rstrip('/')}/vault/list",
            headers=_headers(),
            params={"prefix": prefix, "limit": str(limit)},
            timeout=15,
        )
