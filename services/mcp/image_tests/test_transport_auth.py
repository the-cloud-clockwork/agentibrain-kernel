"""Transport authentication of the built mcp image.

Runs the image named by --mcp-image with docker. CI builds and loads the image
before this suite and pushes it only after the suite passes.
"""

from __future__ import annotations

import http.server
import json
import secrets
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid

import pytest

BRAIN_TOOLS = {
    "brain_feed",
    "brain_get_arc",
    "brain_ingest",
    "brain_search_arcs",
    "brain_tick",
    "kb_brief",
    "kb_search",
    "vault_list",
    "vault_read",
}
FEED_MARKER = "stub-feed-" + uuid.uuid4().hex


class _FeedStub(http.server.BaseHTTPRequestHandler):
    hits: list[str] = []

    def do_GET(self):
        _FeedStub.hits.append(self.path)
        body = json.dumps({"feed": FEED_MARKER}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture(scope="module")
def brain_api():
    server = http.server.ThreadingHTTPServer(("0.0.0.0", 0), _FeedStub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://host.docker.internal:{server.server_address[1]}"
    server.shutdown()


def _docker(*args: str, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)


def _start(image: str, env: dict[str, str]) -> tuple[str, str]:
    name = "mcp-auth-" + uuid.uuid4().hex[:12]
    flags = [f"--env={k}={v}" for k, v in env.items()]
    run = _docker(
        "run",
        "--detach",
        f"--name={name}",
        "--add-host=host.docker.internal:host-gateway",
        "--publish=127.0.0.1::8080",
        *flags,
        image,
    )
    assert run.returncode == 0, run.stderr
    port = _docker("port", name, "8080").stdout.split(":")[-1].strip()
    base = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{base}/ping", timeout=2) as resp:
                if resp.status == 200:
                    return name, base
        except (urllib.error.URLError, ConnectionError, TimeoutError):
            pass
        state = _docker("inspect", "--format={{.State.Running}}", name).stdout.strip()
        if state != "true":
            logs = _docker("logs", name)
            _docker("rm", "--force", name)
            pytest.fail(f"container stopped before ready: {logs.stdout}{logs.stderr}")
        time.sleep(0.5)
    _docker("rm", "--force", name)
    pytest.fail("container never answered /ping")


def _rpc(base: str, method: str, params: dict | None = None, key: str | None = None):
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if key is not None:
        headers["X-API-Key"] = key
    req = urllib.request.Request(f"{base}/mcp", data=json.dumps(payload).encode(), headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            text = resp.read().decode()
            status = resp.status
    except urllib.error.HTTPError as err:
        return err.code, None
    for line in text.splitlines():
        if line.startswith("data:"):
            return status, json.loads(line[5:])
    return status, json.loads(text)


def _refusal(image: str, env: dict[str, str]) -> subprocess.CompletedProcess:
    name = "mcp-auth-" + uuid.uuid4().hex[:12]
    flags = [f"--env={k}={v}" for k, v in env.items()]
    try:
        return _docker("run", f"--name={name}", *flags, image, timeout=30)
    except subprocess.TimeoutExpired:
        pytest.fail("the image started serving instead of refusing")
    finally:
        _docker("rm", "--force", name)


@pytest.fixture(scope="module")
def key() -> str:
    return secrets.token_hex(16)


@pytest.fixture(scope="module")
def required(image, brain_api, key):
    name, base = _start(image, {"MCP_PROXY_API_KEY": key, "BRAIN_API_URL": brain_api})
    yield name, base
    _docker("rm", "--force", name)


@pytest.fixture(scope="module")
def local(image, brain_api):
    name, base = _start(image, {"MCP_AUTH_MODE": "local", "BRAIN_API_URL": brain_api})
    yield base
    _docker("rm", "--force", name)


def test_default_mode_without_key_refuses_to_start(image):
    run = _refusal(image, {})
    assert run.returncode != 0
    assert "MCP_PROXY_API_KEY" in run.stderr


def test_required_mode_with_empty_key_refuses_to_start(image):
    run = _refusal(image, {"MCP_AUTH_MODE": "required", "MCP_PROXY_API_KEY": ""})
    assert run.returncode != 0
    assert "MCP_PROXY_API_KEY" in run.stderr


@pytest.mark.parametrize("mode", ["open", ""])
def test_unknown_mode_refuses_to_start(image, key, mode):
    run = _refusal(image, {"MCP_AUTH_MODE": mode, "MCP_PROXY_API_KEY": key})
    assert run.returncode != 0
    assert "MCP_AUTH_MODE" in run.stderr


def test_ping_needs_no_key(required):
    _, base = required
    with urllib.request.urlopen(f"{base}/ping", timeout=5) as resp:
        assert resp.status == 200


@pytest.mark.parametrize("client_key", [None, "wrong"])
def test_unauthenticated_clients_cannot_list_or_call(required, client_key):
    _, base = required
    hits = len(_FeedStub.hits)
    assert _rpc(base, "tools/list", key=client_key) == (401, None)
    call = {"name": "brain_feed", "arguments": {}}
    assert _rpc(base, "tools/call", call, key=client_key) == (401, None)
    assert len(_FeedStub.hits) == hits


def test_correct_key_lists_brain_tools_and_calls_feed(required, key):
    _, base = required
    status, listed = _rpc(base, "tools/list", key=key)
    assert status == 200
    assert {tool["name"] for tool in listed["result"]["tools"]} == BRAIN_TOOLS
    hits = len(_FeedStub.hits)
    status, called = _rpc(base, "tools/call", {"name": "brain_feed", "arguments": {}}, key=key)
    assert status == 200
    assert FEED_MARKER in called["result"]["content"][0]["text"]
    assert _FeedStub.hits[hits:] == ["/feed"]


def test_key_stays_off_process_command_lines(required, key):
    name, _ = required
    argv = _docker("exec", name, "sh", "-c", "cat /proc/[0-9]*/cmdline | tr '\\0' ' '")
    assert argv.returncode == 0, argv.stderr
    assert "mcp-proxy" in argv.stdout
    assert key not in argv.stdout


def test_local_mode_serves_without_key(local):
    status, listed = _rpc(local, "tools/list")
    assert status == 200
    assert {tool["name"] for tool in listed["result"]["tools"]} == BRAIN_TOOLS


def test_local_mode_enforces_a_key_when_set(image, key):
    name, base = _start(image, {"MCP_AUTH_MODE": "local", "MCP_PROXY_API_KEY": key})
    try:
        assert _rpc(base, "tools/list") == (401, None)
        assert _rpc(base, "tools/list", key=key)[0] == 200
    finally:
        _docker("rm", "--force", name)
