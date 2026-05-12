#!/usr/bin/env python3
"""agentibrain-mcp — Knowledge Base + Brain retrieval tools as a FastMCP server.

Exposes the kernel's read surface to agents over MCP. Tools query
agentibrain-embeddings (pgvector), brain-api (vault text via /vault/search),
and the inference-gateway for LLM synthesis. All paths are HTTP — no
filesystem coupling.
"""

from mcp.server.fastmcp import FastMCP

from tools.arcs import register as register_arcs
from tools.ingest import register as register_ingest
from tools.kb import register as register_kb

mcp = FastMCP("agentibrain")
register_arcs(mcp)
register_ingest(mcp)
register_kb(mcp)


if __name__ == "__main__":
    mcp.run(transport="stdio")
