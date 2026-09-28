"""`agentibrain fix` building blocks: port clash detection, .env rewrite, broken containers."""

import json
import subprocess

from agentibrain import bootstrap


def test_compose_port_vars_reads_defaults():
    text = '- "127.0.0.1:${PORT_POSTGRES:-5432}:5432"\n- "${BIND_HOST:-0.0.0.0}:${PORT_MCP:-8104}:8080"'
    assert bootstrap.compose_port_vars(text) == {"PORT_POSTGRES": 5432, "PORT_MCP": 8104}


def test_port_conflicts_skips_own_containers_and_flags_others(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("PORT_MCP=9104\n")
    monkeypatch.setattr(
        bootstrap,
        "_published_ports",
        lambda: {5432: "other-postgres", 8103: "agentibrain_brain_api"},
    )
    monkeypatch.setattr(bootstrap, "_port_bindable", lambda p: p != 9104)
    found = bootstrap.port_conflicts(
        {"PORT_POSTGRES": 5432, "PORT_BRAIN_API": 8103, "PORT_MCP": 8104}, env
    )
    assert found == [
        ("PORT_POSTGRES", 5432, "other-postgres"),
        ("PORT_MCP", 9104, "a host process"),
    ]


def test_free_port_skips_taken_held_and_unbindable(monkeypatch):
    monkeypatch.setattr(bootstrap, "_published_ports", lambda: {5434: "x"})
    monkeypatch.setattr(bootstrap, "_port_bindable", lambda p: p != 5435)
    assert bootstrap.free_port(5432, {5433}) == 5436


def test_set_env_values_replaces_active_lines_and_appends(tmp_path):
    env = tmp_path / ".env"
    env.write_text("# PORT_POSTGRES=1\nPORT_POSTGRES=5432\nKB_ROUTER_TOKEN=t\n")
    bootstrap.set_env_values(env, {"PORT_POSTGRES": "5433", "PORT_REDIS": "6380"})
    assert env.read_text() == (
        "# PORT_POSTGRES=1\nPORT_POSTGRES=5433\nKB_ROUTER_TOKEN=t\nPORT_REDIS=6380\n"
    )
    assert env.stat().st_mode & 0o777 == 0o600


def _container(service, status="running", error="", networks=None, health=None):
    state = {"Status": status, "Error": error}
    if health:
        state["Health"] = {"Status": health}
    return {
        "Name": f"/agentibrain_{service}",
        "Config": {"Labels": {"com.docker.compose.service": service}},
        "State": state,
        "HostConfig": {"NetworkMode": "kernel_default"},
        "NetworkSettings": {"Networks": {} if networks is None else networks},
    }


def test_broken_services_detects_failed_detached_and_unhealthy(tmp_path, monkeypatch):
    inspected = [
        _container("postgres", networks={}),
        _container("redis", networks={"kernel_default": {}}),
        _container("brain_api", status="created", error="Bind for 0.0.0.0:8103 failed"),
        _container("embeddings", networks={"kernel_default": {}}, health="unhealthy"),
    ]
    monkeypatch.setattr(
        bootstrap,
        "_kernel_containers",
        lambda: (
            [[c["Name"][1:], "kernel", str(tmp_path)] for c in inspected]
            + [["agentibrain_mcp", "other", "/elsewhere"]]
        ),
    )
    calls = []

    def fake_run(cmd, **_):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, json.dumps(inspected), "")

    monkeypatch.setattr(bootstrap.subprocess, "run", fake_run)
    assert bootstrap.broken_services(tmp_path) == [
        ("postgres", "running without its network kernel_default"),
        ("brain_api", "start failed: Bind for 0.0.0.0:8103 failed"),
        ("embeddings", "unhealthy"),
    ]
    assert "agentibrain_mcp" not in calls[0]
