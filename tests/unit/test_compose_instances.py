"""SV2-MUL-02: two named brains run as independent compose projects on one host.

The fixture generates a personal and a swarm compose project, first on
conflicting published ports and then on distinct ones. Docker is never
called: the host view (published ports, project working dirs) and every
compose invocation are faked at the bootstrap seam.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from agentibrain import bootstrap, cli
from agentibrain.config import BrainSettings

PORTS = ("port_brain_api", "port_postgres", "port_redis", "port_mcp")
CONFLICTING = {"personal": (18103, 15432, 16379, 18104), "swarm": (18103, 15432, 16379, 18104)}
DISTINCT = {"personal": (18103, 15432, 16379, 18104), "swarm": (28103, 25432, 26379, 28104)}
SECOND_RUN = {"personal": (38103, 35432, 36379, 38104), "swarm": (48103, 45432, 46379, 48104)}
# Captured before the autouse conftest stub; a named brain resolves only inside tmp_path.
FIND_DEPLOYMENT = bootstrap.find_deployment


def _instance(root: Path, brain_id: str, ports: tuple[int, ...]) -> BrainSettings:
    settings = BrainSettings(
        brain_id=brain_id,
        config_dir=root / brain_id,
        vault_path=root / f"{brain_id}-vault",
        kb_router_token="fixture-bearer-never-printed",
        _env_file=None,
        **dict(zip(PORTS, ports, strict=True)),
    )
    bootstrap.write_compose(settings, bootstrap.render_compose(settings))
    return settings


def _pair(root: Path, layout: dict[str, tuple[int, ...]]) -> dict[str, BrainSettings]:
    return {name: _instance(root, name, ports) for name, ports in layout.items()}


def _rendered(settings: BrainSettings) -> dict:
    return yaml.safe_load((settings.config_dir / "compose.yml").read_text())


def _host_ports(data: dict) -> set[int]:
    return {
        int(port.split(":")[-2])
        for service in data["services"].values()
        for port in service.get("ports", [])
    }


def _running(settings: BrainSettings) -> dict[int, str]:
    """Host port to container name for a stack that is up."""
    project = settings.compose_project
    return {
        port: f"{project}-{service}-1"
        for service, body in _rendered(settings)["services"].items()
        for port in _host_ports({"services": {service: body}})
    }


class FakeHost:
    def __init__(self, monkeypatch):
        self.held: dict[int, str] = {}
        self.workdirs: dict[str, set[str]] = {}
        self.calls: list[tuple[str, tuple[str, ...]]] = []
        self.removed: list[object] = []
        monkeypatch.setattr(bootstrap, "_published_ports", lambda: dict(self.held))
        monkeypatch.setattr(bootstrap, "_port_bindable", lambda port: port not in self.held)
        monkeypatch.setattr(
            bootstrap, "_project_workdirs", lambda project: set(self.workdirs.get(project, ()))
        )
        monkeypatch.setattr(bootstrap, "_docker_compose", self._compose)
        monkeypatch.setattr(bootstrap, "compose_stream", self._stream)
        monkeypatch.setattr(bootstrap, "run_migrations", lambda settings: [])
        monkeypatch.setattr(bootstrap, "remove_other_stacks", self._remove)
        monkeypatch.setattr(bootstrap, "find_deployment", FIND_DEPLOYMENT)
        self.vaults: dict[str, set[str]] = {}
        monkeypatch.setattr(
            bootstrap, "_vault_projects", lambda vault: set(self.vaults.get(vault, ()))
        )

    def start(self, settings: BrainSettings) -> None:
        self.held.update(_running(settings))
        self.workdirs.setdefault(settings.compose_project, set()).add(str(settings.config_dir))
        vault = str(settings.vault_path.resolve())
        self.vaults.setdefault(vault, set()).add(settings.compose_project)

    def _compose(self, cmd, cwd):
        self.calls.append((str(cwd), tuple(cmd)))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    def _stream(self, cmd, cwd):
        self.calls.append((str(cwd), tuple(cmd)))
        return 0

    def _remove(self, keep):
        self.removed.append(keep)
        return []


def _cli(monkeypatch, settings: BrainSettings, *args: str):
    monkeypatch.setattr(cli, "_load_settings", lambda: settings)
    return CliRunner().invoke(cli.main, list(args))


@pytest.mark.parametrize("layout", [DISTINCT, SECOND_RUN])
def test_named_brains_render_independent_projects(tmp_path, layout):
    pair = _pair(tmp_path, layout)
    personal, swarm = (_rendered(pair[n]) for n in ("personal", "swarm"))

    assert personal["name"] == "agentibrain-personal"
    assert swarm["name"] == "agentibrain-swarm"
    for data in (personal, swarm):
        assert all("container_name" not in s for s in data["services"].values())
    assert _host_ports(personal).isdisjoint(_host_ports(swarm))
    assert _host_ports(swarm) == set(layout["swarm"])
    volumes = {f"{d['name']}_{v}" for d in (personal, swarm) for v in d["volumes"]}
    assert len(volumes) == len(personal["volumes"]) + len(swarm["volumes"])


def test_service_urls_stay_on_the_project_network(tmp_path):
    data = _rendered(_instance(tmp_path, "swarm", DISTINCT["swarm"]))
    services = set(data["services"])
    for body in data["services"].values():
        assert body["networks"] == ["agentibrain_net"]
        for value in (body.get("environment") or {}).values():
            for host in re.findall(r"://(?:[^@/]*@)?([a-z][a-z0-9_-]*):\d+", str(value)):
                assert host in services, (host, value)


def test_the_default_brain_keeps_its_legacy_names(tmp_path):
    settings = BrainSettings(
        config_dir=tmp_path / "default", vault_path=tmp_path / "v", _env_file=None
    )
    data = yaml.safe_load(bootstrap.render_compose(settings))
    assert settings.compose_project is None
    assert "name" not in data
    assert data["services"]["brain-api"]["container_name"] == "agentibrain_brain_api"
    assert _host_ports(data) == {5432, 6379, 8103, 8104}


def test_a_named_brain_resolves_only_its_own_deployment(tmp_path, monkeypatch):
    swarm = _instance(tmp_path, "swarm", DISTINCT["swarm"])
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / "compose.yml").write_text(f"services: {{}}\n# {bootstrap.COMPOSE_MARKER}\n")
    monkeypatch.setattr(
        bootstrap,
        "_kernel_containers",
        lambda: [[bootstrap.COMPOSE_MARKER, "agentibrain", str(checkout)]],
    )
    assert FIND_DEPLOYMENT(swarm, cwd=checkout) == ("home", swarm.config_dir)


def test_starting_both_brains_creates_two_running_stacks(tmp_path, monkeypatch):
    host = FakeHost(monkeypatch)
    pair = _pair(tmp_path, DISTINCT)
    before = bootstrap.brain_instance_resource_conflicts["port"]
    for name in ("personal", "swarm"):
        result = _cli(monkeypatch, pair[name], "up")
        assert result.exit_code == 0, result.output
        host.start(pair[name])

    assert host.removed == []
    assert {cwd for cwd, _ in host.calls} == {str(s.config_dir) for s in pair.values()}
    assert all("down" not in cmd for _, cmd in host.calls)
    assert bootstrap.brain_instance_resource_conflicts["port"] == before


def test_conflicting_ports_refuse_without_touching_the_other_stack(tmp_path, monkeypatch):
    host = FakeHost(monkeypatch)
    pair = _pair(tmp_path, CONFLICTING)
    host.start(pair["personal"])
    held_before = dict(host.held)
    before = bootstrap.brain_instance_resource_conflicts["port"]

    result = _cli(monkeypatch, pair["swarm"], "up")

    assert result.exit_code == 1
    assert "fixture-bearer-never-printed" not in result.output
    assert bootstrap.instance_conflicts(pair["swarm"]) == [
        ("port", "PORT_BRAIN_API 18103 is held by agentibrain-personal-brain-api-1"),
        ("port", "PORT_POSTGRES 15432 is held by agentibrain-personal-postgres-1"),
        ("port", "PORT_REDIS 16379 is held by agentibrain-personal-redis-1"),
        ("port", "PORT_MCP 18104 is held by agentibrain-personal-mcp-1"),
    ]
    assert host.calls == []
    assert host.removed == []
    assert host.held == held_before
    assert bootstrap.brain_instance_resource_conflicts["port"] - before == len(PORTS)


def test_the_same_project_from_another_config_root_is_refused(tmp_path, monkeypatch):
    host = FakeHost(monkeypatch)
    first = _instance(tmp_path / "a", "swarm", DISTINCT["swarm"])
    second = _instance(tmp_path / "b", "swarm", SECOND_RUN["swarm"])
    host.start(first)

    conflicts = bootstrap.instance_conflicts(second)

    assert [kind for kind, _ in conflicts] == ["project"]
    assert str(first.config_dir) in conflicts[0][1]
    assert _cli(monkeypatch, second, "up").exit_code != 0
    assert host.calls == []


def test_a_brain_whose_name_extends_another_is_not_its_own(tmp_path, monkeypatch):
    host = FakeHost(monkeypatch)
    longer = _instance(tmp_path, "a-b", DISTINCT["personal"])
    shorter = _instance(tmp_path, "a", DISTINCT["personal"])
    host.start(longer)

    conflicts = bootstrap.instance_conflicts(shorter)

    assert ("port", "PORT_BRAIN_API 18103 is held by agentibrain-a-b-brain-api-1") in conflicts
    assert len(conflicts) == len(PORTS)


def test_a_shared_vault_is_a_volume_conflict(tmp_path, monkeypatch):
    host = FakeHost(monkeypatch)
    personal = _instance(tmp_path, "personal", DISTINCT["personal"])
    swarm = BrainSettings(
        brain_id="swarm",
        config_dir=tmp_path / "swarm",
        vault_path=personal.vault_path,
        _env_file=None,
        **dict(zip(PORTS, DISTINCT["swarm"], strict=True)),
    )
    bootstrap.write_compose(swarm, bootstrap.render_compose(swarm))
    host.start(personal)
    vault = str(personal.vault_path.resolve())

    assert _rendered(swarm)["services"]["brain-api"]["labels"] == {"agentibrain.vault": vault}
    assert bootstrap.instance_conflicts(swarm) == [
        ("volume", f"vault {vault} is mounted by compose project agentibrain-personal")
    ]
    assert _cli(monkeypatch, swarm, "up").exit_code == 1
    assert host.calls == []


def test_up_publishes_ports_edited_after_install(tmp_path, monkeypatch):
    FakeHost(monkeypatch)
    _instance(tmp_path, "swarm", DISTINCT["swarm"])
    edited = BrainSettings(
        brain_id="swarm",
        config_dir=tmp_path / "swarm",
        vault_path=tmp_path / "swarm-vault",
        _env_file=None,
        **dict(zip(PORTS, SECOND_RUN["swarm"], strict=True)),
    )

    assert _cli(monkeypatch, edited, "up").exit_code == 0
    assert _host_ports(_rendered(edited)) == set(SECOND_RUN["swarm"])


def test_corrected_ports_need_a_new_request(tmp_path, monkeypatch):
    host = FakeHost(monkeypatch)
    conflicting = _pair(tmp_path / "conflict", CONFLICTING)
    host.start(conflicting["personal"])
    assert _cli(monkeypatch, conflicting["swarm"], "up").exit_code != 0

    fixed = _instance(tmp_path / "conflict", "swarm", DISTINCT["swarm"])
    result = _cli(monkeypatch, fixed, "up")

    assert result.exit_code == 0, result.output
    assert {cwd for cwd, _ in host.calls} == {str(fixed.config_dir)}


def test_restarting_one_brain_leaves_the_other_serving(tmp_path, monkeypatch):
    host = FakeHost(monkeypatch)
    pair = _pair(tmp_path, DISTINCT)
    for settings in pair.values():
        host.start(settings)
    personal_ports = {p: n for p, n in host.held.items() if n.startswith("agentibrain-personal-")}

    assert bootstrap.instance_conflicts(pair["swarm"]) == []
    for command in ("down", "up"):
        result = _cli(monkeypatch, pair["swarm"], command)
        assert result.exit_code == 0, result.output

    assert host.removed == []
    assert {cwd for cwd, _ in host.calls} == {str(pair["swarm"].config_dir)}
    assert all(host.held[p] == n for p, n in personal_ports.items())


def test_the_default_brain_still_replaces_only_legacy_stacks(tmp_path, monkeypatch):
    host = FakeHost(monkeypatch)
    settings = BrainSettings(
        config_dir=tmp_path / "default", vault_path=tmp_path / "v", _env_file=None
    )
    bootstrap.write_compose(settings, bootstrap.render_compose(settings))
    monkeypatch.setattr(
        bootstrap, "find_deployment", lambda *a, **kw: ("home", settings.config_dir)
    )

    assert _cli(monkeypatch, settings, "up").exit_code == 0
    assert host.removed == [settings.config_dir]
