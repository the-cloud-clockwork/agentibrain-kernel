import json
import sys
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.tools import ingest


class Registry:
    def tool(self):
        def register(fn):
            self.brain_ingest = fn
            return fn

        return register


@pytest.mark.asyncio
@pytest.mark.parametrize("repository", [None, "https://github.com/example/requested"])
@pytest.mark.parametrize("chunk_size", [200_000, 20])
async def test_repository_field_requires_explicit_tool_input(monkeypatch, repository, chunk_size):
    forms = []

    async def receive(request):
        forms.append(dict(await request.post()))
        return web.json_response({"errors": []})

    app = web.Application()
    app.router.add_post("/ingest", receive)
    async with TestServer(app) as server:
        monkeypatch.setattr(ingest, "BRAIN_API_URL", str(server.make_url("")))
        registry = Registry()
        ingest.register(registry)
        fields = {} if repository is None else {"repository": repository}
        content = "CI findings mention https://github.com/example/unrelated"
        result = json.loads(await registry.brain_ingest(content, chunk_size=chunk_size, **fields))

    assert result["chunks_sent"] == (1 if chunk_size == 200_000 else 3)
    assert [form["repository"] for form in forms if "repository" in form] == (
        [] if repository is None else [repository]
    )
    assert all(form["producer"] == "agent" for form in forms)
    if chunk_size == 200_000:
        assert forms[0]["message"] == content
    else:
        assert forms[0]["message"] == "[Part 1/3 of document]\n\nCI findings mention "
        assert forms[1]["message"] == "[Part 2/3 of document]\n\nhttps://github.com/e"
        assert forms[2]["message"] == "[Part 3/3 of document]\n\nxample/unrelated"
