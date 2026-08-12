"""Direct vault access over the MCP.

These exist because an agent asked for `brain-feed/lessons.md`, had no tool that
could open it, and fell back to guessing filesystem paths on the host — where
the vault does not live. Search answers "where might this be"; these answer
"show me this", and an agent that already knows the path needs the second one.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_APP = Path(__file__).resolve().parents[1] / "app"
if str(_APP) not in sys.path:
    sys.path.insert(0, str(_APP))

from tools import vault  # noqa: E402


class _Recorder:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    async def __call__(self, method, url, headers=None, params=None, body=None, timeout=10):
        self.calls.append({"method": method, "url": url, "params": params, "headers": headers})
        return json.dumps(self.payload)


class _Registry:
    """Minimal stand-in for FastMCP: captures the functions register() adds."""

    def __init__(self):
        self.tools = {}

    def tool(self, *a, **k):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn

        return deco


@pytest.fixture()
def tools(monkeypatch):
    reg = _Registry()
    vault.register(reg)
    return reg.tools


@pytest.fixture()
def recorder(monkeypatch):
    rec = _Recorder({"ok": True})
    monkeypatch.setattr(vault, "http_request", rec)
    return rec


async def test_the_three_tools_are_registered(tools):
    assert set(tools) == {"brain_feed", "vault_read", "vault_list"}


async def test_vault_read_targets_the_api_not_the_local_filesystem(tools, recorder):
    """The vault is mounted inside brain-api and generally does not exist on the
    machine running the agent, so a local read cannot succeed by construction."""
    await tools["vault_read"]("brain-feed/lessons.md")

    call = recorder.calls[0]
    assert call["url"].endswith("/vault/read")
    assert call["params"]["path"] == "brain-feed/lessons.md"


async def test_brain_feed_reads_the_feed_endpoint(tools, recorder):
    """The authoritative answer to "what are my latest lessons" — no search, no
    embedding, no guess about phrasing."""
    await tools["brain_feed"]()

    assert recorder.calls[0]["url"].endswith("/feed")
    assert recorder.calls[0]["method"] == "GET"


async def test_vault_list_passes_its_prefix(tools, recorder):
    await tools["vault_list"]("left/reference", limit=50)

    call = recorder.calls[0]
    assert call["url"].endswith("/vault/list")
    assert call["params"] == {"prefix": "left/reference", "limit": "50"}


async def test_the_token_travels_when_one_is_configured(tools, recorder, monkeypatch):
    monkeypatch.setattr(vault, "BRAIN_API_TOKEN", "t0ken")
    await tools["vault_read"]("brain-feed/signals.md")

    assert recorder.calls[0]["headers"] == {"Authorization": "Bearer t0ken"}


async def test_no_token_sends_no_auth_header(tools, recorder, monkeypatch):
    monkeypatch.setattr(vault, "BRAIN_API_TOKEN", "")
    await tools["vault_list"]()

    assert recorder.calls[0]["headers"] == {}
