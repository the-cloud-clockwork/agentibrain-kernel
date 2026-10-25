"""SV2-MUL-03: CLI lifecycle commands act only on the explicitly selected brain.

The fixture holds two named brains, personal and swarm, an unowned container
whose name looks like a legacy kernel container, and an installation that
stopped after writing its config and bearer but before rendering compose.
Docker is never called: the container listing, every compose invocation and
the agentihooks link calls are faked at the CLI and bootstrap seams.
"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from agentibrain import bootstrap, cli
from agentibrain import hooks_env as _hooks_env
from agentibrain.config import BrainSettings

FIND_DEPLOYMENT = bootstrap.find_deployment
TOKENS = {"personal": "personal-bearer-never-printed", "swarm": "swarm-bearer-never-printed"}
LAYOUTS = {
    "first": {"personal": 18103, "swarm": 28103},
    "second": {"personal": 38103, "swarm": 48103},
}
LOOKALIKE = "agentibrain_postgres"


def _env_lines(brain_id: str, port: int) -> str:
    return (
        f"KB_ROUTER_TOKEN={TOKENS[brain_id]}\nBRAIN_ID={brain_id}\n"
        f"PORT_BRAIN_API={port}\nPORT_POSTGRES={port + 1}\n"
        f"PORT_REDIS={port + 2}\nPORT_MCP={port + 3}\n"
    )


def _root(brain_id: str) -> Path:
    return Path.home() / f".agentibrain-{brain_id}"


def _instance(brain_id: str, port: int, *, interrupted: bool = False) -> Path:
    root = _root(brain_id)
    settings = BrainSettings(
        brain_id=brain_id,
        config_dir=root,
        vault_path=Path.home() / f"{brain_id}-vault",
        _env_file=None,
    )
    bootstrap.write_config(settings)
    (root / ".env").write_text(_env_lines(brain_id, port))
    if not interrupted:
        loaded = BrainSettings(
            brain_id=brain_id,
            config_dir=root,
            vault_path=settings.vault_path,
            _env_file=str(root / ".env"),
        )
        bootstrap.write_compose(loaded, bootstrap.render_compose(loaded))
    return root


def _snapshot(root: Path) -> dict[str, str]:
    return {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.iterdir())
        if p.is_file()
    }


class FakeHost:
    def __init__(self, monkeypatch, tmp_path: Path):
        self.containers: list[list[str]] = []
        self.calls: list[tuple[str, tuple[str, ...]]] = []
        self.links: list[tuple[str, ...]] = []
        self.swept: list[bool] = []
        lookalike = tmp_path / "lookalike"
        lookalike.mkdir()
        (lookalike / "compose.yml").write_text("services:\n  postgres:\n    image: postgres\n")
        self.lookalike = [LOOKALIKE, "lookalike", str(lookalike)]
        self.containers.append(self.lookalike)
        monkeypatch.setattr(bootstrap, "_kernel_containers", lambda: list(self.containers))
        monkeypatch.setattr(bootstrap, "_published_ports", lambda: {})
        monkeypatch.setattr(bootstrap, "_port_bindable", lambda port: True)
        monkeypatch.setattr(bootstrap, "_project_workdirs", lambda project: set())
        monkeypatch.setattr(bootstrap, "_vault_projects", lambda vault: set())
        monkeypatch.setattr(bootstrap, "_docker_compose", self._compose)
        monkeypatch.setattr(bootstrap, "compose_stream", self._stream)
        monkeypatch.setattr(bootstrap, "run_migrations", lambda settings: [])
        monkeypatch.setattr(bootstrap, "find_deployment", FIND_DEPLOYMENT)
        monkeypatch.setattr(cli, "_agentihooks_bin", lambda: "agentihooks")
        monkeypatch.setattr(cli.subprocess, "run", self._run)
        monkeypatch.setattr(_hooks_env, "sweep_managed_file", self._sweep)

    def legacy(self, project: str, workdir: Path, *names: str) -> None:
        workdir.mkdir(parents=True, exist_ok=True)
        (workdir / "compose.yml").write_text(f"services: {{}}\n# {bootstrap.COMPOSE_MARKER}\n")
        self.containers += [[name, project, str(workdir)] for name in names]

    def _compose(self, cmd, cwd):
        self.calls.append((str(cwd), tuple(cmd)))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    def _stream(self, cmd, cwd):
        self.calls.append((str(cwd), tuple(cmd)))
        return 0

    def _run(self, cmd, *args, **kwargs):
        self.links.append(tuple(cmd))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    def _sweep(self):
        self.swept.append(True)
        return None


def _cli(*args: str):
    return CliRunner().invoke(cli.main, list(args))


@pytest.fixture
def host(monkeypatch, tmp_path):
    work = tmp_path / "cwd"
    work.mkdir()
    monkeypatch.chdir(work)
    return FakeHost(monkeypatch, tmp_path)


@pytest.mark.parametrize("layout", ["first", "second"])
def test_down_for_personal_leaves_swarm_untouched(host, layout):
    roots = {name: _instance(name, port) for name, port in LAYOUTS[layout].items()}
    swarm_before = _snapshot(roots["swarm"])
    before = bootstrap.brain_cross_instance_mutation_rejections.copy()

    result = _cli("--instance", "personal", "down")

    assert result.exit_code == 0, result.output
    assert host.calls == [(str(roots["personal"]), ("--env-file", ".env", "down"))]
    assert _snapshot(roots["swarm"]) == swarm_before
    assert host.links == []
    assert bootstrap.brain_cross_instance_mutation_rejections == before


def test_uninstall_unlinks_only_the_selected_brain(host):
    roots = {name: _instance(name, port) for name, port in LAYOUTS["first"].items()}
    swarm_before = _snapshot(roots["swarm"])

    result = _cli("--instance", "personal", "uninstall")

    assert result.exit_code == 0, result.output
    assert host.calls == [(str(roots["personal"]), ("--env-file", ".env", "down"))]
    assert host.links == [("agentihooks", "link-profile", "unlink", "brain-personal")]
    assert _snapshot(roots["swarm"]) == swarm_before
    assert TOKENS["personal"] not in result.output


@pytest.mark.parametrize("command", ["up", "build", "logs", "down"])
def test_every_lifecycle_command_runs_in_the_selected_root(host, command):
    roots = {name: _instance(name, port) for name, port in LAYOUTS["first"].items()}

    result = _cli("--instance", "swarm", command)

    assert result.exit_code == 0, result.output
    assert host.calls
    assert {cwd for cwd, _ in host.calls} == {str(roots["swarm"])}
    assert not any("-p" in cmd for _, cmd in host.calls)


@pytest.mark.parametrize("brain_id", ["personal", "swarm"])
def test_check_and_sync_read_the_selected_brain(host, brain_id):
    roots = {name: _instance(name, port) for name, port in LAYOUTS["first"].items()}
    with cli.main.make_context("agentibrain", ["--instance", brain_id, "check"]) as ctx:
        settings = cli._load_settings()
    assert ctx.params["instance"] == brain_id
    assert settings.brain_id == brain_id
    assert settings.config_dir == roots[brain_id]
    assert settings.brain_url == f"http://localhost:{LAYOUTS['first'][brain_id]}"


def test_the_instance_env_var_selects_like_the_option(host, monkeypatch):
    roots = {name: _instance(name, port) for name, port in LAYOUTS["first"].items()}
    monkeypatch.setenv("AGENTIBRAIN_INSTANCE", "swarm")

    assert _cli("down").exit_code == 0
    assert {cwd for cwd, _ in host.calls} == {str(roots["swarm"])}


def test_an_ambiguous_legacy_selection_refuses(host, tmp_path):
    host.legacy("checkout-a", tmp_path / "a", "agentibrain_redis")
    host.legacy("checkout-b", tmp_path / "b", bootstrap.COMPOSE_MARKER)
    before = bootstrap.brain_cross_instance_mutation_rejections["ambiguous"]

    result = _cli("down")

    assert result.exit_code == 2
    assert "checkout-a" in result.output and "checkout-b" in result.output
    assert host.calls == []
    assert bootstrap.brain_cross_instance_mutation_rejections["ambiguous"] - before == 1


def test_an_explicit_default_selection_stops_only_its_own_stack(host, tmp_path):
    host.legacy("checkout-a", tmp_path / "a", "agentibrain_redis")
    host.legacy("checkout-b", tmp_path / "b", bootstrap.COMPOSE_MARKER)

    result = _cli("--instance", "default", "down")

    assert result.exit_code == 0, result.output
    assert host.calls == [(str(tmp_path / "b"), ("down",))]


def test_the_lookalike_container_is_never_stopped(host, tmp_path):
    host.legacy("checkout-a", tmp_path / "a", "agentibrain_redis")
    host.legacy("checkout-b", tmp_path / "b", bootstrap.COMPOSE_MARKER)
    before = bootstrap.brain_cross_instance_mutation_rejections["unowned"]

    result = _cli("--instance", "default", "up")

    assert result.exit_code == 0, result.output
    stopped = [cmd for _, cmd in host.calls if "down" in cmd]
    assert stopped == [("-p", "checkout-a", "down", "--remove-orphans")]
    assert bootstrap.brain_cross_instance_mutation_rejections["unowned"] - before == 1


def test_a_root_holding_another_brain_refuses(host):
    _instance("personal", LAYOUTS["first"]["personal"])
    misbound = _root("swarm")
    misbound.mkdir()
    (misbound / ".env").write_text(_env_lines("personal", LAYOUTS["first"]["swarm"]))
    protected = _snapshot(misbound)
    before = bootstrap.brain_cross_instance_mutation_rejections["instance"]

    result = _cli("--instance", "swarm", "down")

    assert result.exit_code == 2
    assert "personal" in result.output
    assert TOKENS["personal"] not in result.output
    assert host.calls == []
    assert _snapshot(misbound) == protected
    assert bootstrap.brain_cross_instance_mutation_rejections["instance"] - before == 1


@pytest.mark.parametrize(
    "args",
    [["down"], ["up"], ["build"], ["uninstall"]],
)
def test_dry_run_reports_an_inventory_without_mutating(host, args):
    roots = {name: _instance(name, port) for name, port in LAYOUTS["first"].items()}
    before = {name: _snapshot(root) for name, root in roots.items()}

    result = _cli("--instance", "personal", *args, "--dry-run")

    assert result.exit_code == 0, result.output
    assert host.calls == []
    assert host.links == []
    flat = " ".join(result.output.split())
    assert "instance personal" in flat
    assert "agentibrain-personal" in flat
    assert str(roots["personal"]) in flat
    assert {name: _snapshot(root) for name, root in roots.items()} == before


def test_dry_run_names_the_legacy_stack_up_would_replace(host, tmp_path):
    host.legacy("checkout-a", tmp_path / "a", "agentibrain_redis")
    host.legacy("checkout-b", tmp_path / "b", bootstrap.COMPOSE_MARKER)

    result = _cli("--instance", "default", "up", "--dry-run")

    assert result.exit_code == 0, result.output
    flat = " ".join(result.output.split())
    assert "would remove legacy stack checkout-a" in flat
    assert "would remove legacy stack lookalike" not in flat
    assert f"left unowned container {LOOKALIKE}" in flat
    assert host.calls == []


def _install(brain_id: str):
    return _cli("--instance", brain_id, "install", "--no-init")


def test_an_interrupted_install_resumes_for_that_instance_only(host):
    personal = _instance("personal", LAYOUTS["first"]["personal"])
    swarm = _instance("swarm", LAYOUTS["first"]["swarm"], interrupted=True)
    personal_before = _snapshot(personal)
    assert not (swarm / "compose.yml").exists()

    result = _install("swarm")

    assert result.exit_code == 0, result.output
    assert TOKENS["swarm"] not in result.output
    env = bootstrap._existing_assignments(swarm / ".env")
    assert env["KB_ROUTER_TOKEN"] == TOKENS["swarm"]
    assert env["BRAIN_ID"] == "swarm"
    assert "name: agentibrain-swarm" in (swarm / "compose.yml").read_text()
    assert {cwd for cwd, _ in host.calls} == {str(swarm)}
    assert _snapshot(personal) == personal_before
    assert host.links == [
        (
            "agentihooks",
            "link-profile",
            "link",
            str(cli.PROFILES_ROOT / "brain"),
            "--name",
            "brain-swarm",
            "--no-init",
        ),
    ]
    assert host.swept == []


def test_a_resumed_install_is_idempotent(host):
    _instance("personal", LAYOUTS["first"]["personal"])
    swarm = _instance("swarm", LAYOUTS["first"]["swarm"], interrupted=True)
    assert _install("swarm").exit_code == 0
    after_first = _snapshot(swarm)
    calls_first = list(host.calls)

    result = _install("swarm")

    assert result.exit_code == 0, result.output
    assert _snapshot(swarm) == after_first
    assert host.calls[len(calls_first) :] == calls_first
