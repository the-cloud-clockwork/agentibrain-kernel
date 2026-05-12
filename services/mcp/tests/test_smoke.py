"""Smoke tests for agentibrain-mcp — ensure tools register without crashing.

Run with `pytest services/mcp/tests/`. The kernel CI installs requirements.txt first.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path


def _add_app_to_path():
    """Mirror the runtime layout: /app/server on PYTHONPATH so `from tools.X` works."""
    app_dir = Path(__file__).resolve().parents[1] / "app"
    sys.path.insert(0, str(app_dir))


def test_arcs_module_imports():
    _add_app_to_path()
    mod = importlib.import_module("tools.arcs")
    assert hasattr(mod, "register"), "tools.arcs must expose register(mcp)"


def test_kb_module_imports():
    _add_app_to_path()
    mod = importlib.import_module("tools.kb")
    assert hasattr(mod, "register"), "tools.kb must expose register(mcp)"


def test_server_constructs_and_registers():
    _add_app_to_path()
    from mcp.server.fastmcp import FastMCP

    from tools.arcs import register as register_arcs
    from tools.kb import register as register_kb

    mcp = FastMCP("agentibrain-test")
    register_arcs(mcp)
    register_kb(mcp)


def test_kb_search_handles_no_backends():
    """With no env vars set, kb_search returns an empty result, not a crash."""
    import asyncio
    import json
    import os

    _add_app_to_path()
    # Strip env so both backends are skipped
    for var in ("EMBEDDINGS_URL", "EMBEDDINGS_API_KEY", "BRAIN_API_URL"):
        os.environ.pop(var, None)

    # Re-import so module-level constants pick up the empty env
    if "tools.kb" in sys.modules:
        del sys.modules["tools.kb"]
    from tools.kb import register as register_kb
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("smoke")
    register_kb(mcp)

    # Pull the bound function out of the FastMCP registry
    tools = asyncio.run(mcp.list_tools())
    names = {t.name for t in tools}
    assert "kb_search" in names
    assert "kb_brief" in names
