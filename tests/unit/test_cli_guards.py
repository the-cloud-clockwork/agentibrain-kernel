"""CLI deployment detection + guards.

build/up/down/logs/status drive whichever compose deployment exists —
the repo checkout pinned by local/bootstrap.sh (AGENTIBRAIN_REPO), a
checkout found by walking up from cwd, or the stack install rendered in
~/.agentibrain. With none of those, commands exit 2 with a hint instead
of a raw traceback.

Each case runs the CLI in a subprocess with a scratch HOME (config paths
resolve from Path.home() at import time) and a scratch cwd (so the real
repo checkout is never detected). Docker is a shim script on PATH that
records its argv and cwd — no real docker involved.
"""

import subprocess
import sys
from pathlib import Path

import pytest

MARKER_COMPOSE = "services:\n  brain-api:\n    container_name: agentibrain_brain_api\n"


def _run_cli(args: list[str], home: Path, cwd: Path, path: str = "/usr/bin:/bin"):
    code = (
        "import sys; sys.argv = ['agentibrain'] + sys.argv[1:]; "
        "from agentibrain.cli import main; main()"
    )
    return subprocess.run(
        [sys.executable, "-c", code, *args],
        capture_output=True,
        text=True,
        env={"HOME": str(home), "PATH": path},
        cwd=str(cwd),
        timeout=30,
    )


@pytest.fixture
def docker_shim(tmp_path):
    """Fake `docker` on PATH that appends its argv + cwd to a record file."""
    bindir = tmp_path / "shim-bin"
    bindir.mkdir()
    record = tmp_path / "docker-argv.txt"
    shim = bindir / "docker"
    shim.write_text(
        f'#!/bin/sh\necho "$PWD :: $@" >> "{record}"\n'
        f'[ "$1" = ps ] && cat "{tmp_path / "docker-ps.txt"}" 2>/dev/null\n'
        "exit 0\n"
    )
    shim.chmod(0o755)
    return f"{bindir}:/usr/bin:/bin", record


def _home_with_repo(tmp_path) -> tuple[Path, Path]:
    """Scratch HOME whose .agentibrain/.env pins a marker-carrying repo."""
    home = tmp_path / "home"
    (home / ".agentibrain").mkdir(parents=True)
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "compose.yml").write_text(MARKER_COMPOSE)
    (home / ".agentibrain" / ".env").write_text(f"AGENTIBRAIN_REPO={repo}\n")
    return home, repo


@pytest.mark.parametrize("cmd", ["up", "down", "build", "logs"])
def test_commands_exit_cleanly_without_any_deployment(tmp_path, docker_shim, cmd):
    path, _ = docker_shim
    home = tmp_path / "home"
    home.mkdir()
    r = _run_cli([cmd], home, cwd=home, path=path)
    assert r.returncode == 2, r.stderr
    assert "no agentibrain deployment found" in r.stdout
    assert "FileNotFoundError" not in r.stderr


def test_status_degrades_gracefully_without_any_deployment(tmp_path, docker_shim):
    path, _ = docker_shim
    home = tmp_path / "home"
    home.mkdir()
    r = _run_cli(["status"], home, cwd=home, path=path)
    assert r.returncode == 0, r.stderr
    assert "no deployment found" in r.stdout
    assert "FileNotFoundError" not in r.stderr


def test_build_targets_pinned_repo_from_any_cwd(tmp_path, docker_shim):
    path, record = docker_shim
    home, repo = _home_with_repo(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    r = _run_cli(["build"], home, cwd=elsewhere, path=path)
    assert r.returncode == 0, r.stderr
    lines = record.read_text().splitlines()
    assert any(f"{repo} :: compose up -d --build" in ln for ln in lines)


def test_build_forwards_service_names(tmp_path, docker_shim):
    path, record = docker_shim
    home, _ = _home_with_repo(tmp_path)
    r = _run_cli(["build", "brain-api", "embeddings"], home, cwd=home, path=path)
    assert r.returncode == 0, r.stderr
    assert "compose up -d --build brain-api embeddings" in record.read_text()


def test_logs_passthrough_flags(tmp_path, docker_shim):
    path, record = docker_shim
    home, repo = _home_with_repo(tmp_path)
    r = _run_cli(["logs", "tick-cron", "--since", "10m", "--tail", "50"], home, cwd=home, path=path)
    assert r.returncode == 0, r.stderr
    assert f"{repo} :: compose logs --since 10m --tail 50 tick-cron" in record.read_text()


def test_cwd_checkout_beats_stale_pin(tmp_path, docker_shim):
    """Standing inside checkout B must target B, not the pinned checkout A."""
    path, record = docker_shim
    home, pinned_repo = _home_with_repo(tmp_path)
    other = tmp_path / "checkout-b"
    other.mkdir()
    (other / "compose.yml").write_text(MARKER_COMPOSE)
    r = _run_cli(["build"], home, cwd=other, path=path)
    assert r.returncode == 0, r.stderr
    content = record.read_text()
    assert f"{other} :: compose up -d --build" in content
    assert str(pinned_repo) not in content


def test_upward_walk_detects_unpinned_checkout(tmp_path, docker_shim):
    path, record = docker_shim
    home = tmp_path / "home"
    home.mkdir()
    repo = tmp_path / "checkout"
    (repo / "nested" / "deep").mkdir(parents=True)
    (repo / "compose.yml").write_text(MARKER_COMPOSE)
    r = _run_cli(["up"], home, cwd=repo / "nested" / "deep", path=path)
    assert r.returncode == 0, r.stderr
    assert f"{repo} :: compose up -d" in record.read_text()


def test_status_uses_detected_deployment(tmp_path, docker_shim):
    path, record = docker_shim
    home, repo = _home_with_repo(tmp_path)
    r = _run_cli(["status"], home, cwd=home, path=path)
    assert r.returncode == 0, r.stderr
    assert f"{repo} :: compose ps" in record.read_text()
    assert "root-compose" in r.stdout


def test_down_then_up_follow_the_running_checkout(tmp_path, docker_shim):
    """A stack in ~/.agentibrain beside a running checkout captures neither down nor the next up."""
    path, record = docker_shim
    home = tmp_path / "home"
    cfg = home / ".agentibrain"
    cfg.mkdir(parents=True)
    (cfg / "compose.yml").write_text(MARKER_COMPOSE)
    (cfg / ".env").write_text("LOG_LEVEL=INFO\n")
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "compose.yml").write_text(MARKER_COMPOSE)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    (tmp_path / "docker-ps.txt").write_text(f"agentibrain_brain_api\tkernel\t{repo}\n")
    r = _run_cli(["down"], home, cwd=elsewhere, path=path)
    assert r.returncode == 0, r.stderr
    assert f"{repo} :: compose down" in record.read_text()
    assert (cfg / ".env").read_text() == f"LOG_LEVEL=INFO\nAGENTIBRAIN_REPO={repo}\n"

    (tmp_path / "docker-ps.txt").unlink()
    r = _run_cli(["up"], home, cwd=elsewhere, path=path)
    assert r.returncode == 0, r.stderr
    content = record.read_text()
    assert f"{repo} :: compose up -d" in content
    assert f"{cfg} :: " not in content


def test_up_replaces_every_other_stack(tmp_path, docker_shim):
    """Wherever `up` runs, every agentibrain stack but its target is downed first."""
    path, record = docker_shim
    home, repo = _home_with_repo(tmp_path)
    (tmp_path / "docker-ps.txt").write_text(
        f"agentibrain_brain_api\tother-checkout\t{tmp_path / 'other'}\n"
        f"agentibrain_minio\tagentibrain\t{home / '.agentibrain'}\n"
        f"agentibrain_redis\trepo\t{repo}\n"
    )
    r = _run_cli(["up"], home, cwd=repo, path=path)
    assert r.returncode == 0, r.stderr
    lines = record.read_text().splitlines()
    downs = [i for i, ln in enumerate(lines) if "down --remove-orphans" in ln]
    assert any("compose -p other-checkout down --remove-orphans" in ln for ln in lines)
    assert any("compose -p agentibrain down --remove-orphans" in ln for ln in lines)
    assert not any("-p repo down" in ln for ln in lines)
    assert max(downs) < lines.index(f"{repo} :: compose up -d")


def test_checkout_without_env_is_linked_to_the_brain_env(tmp_path, docker_shim):
    """compose reads the project .env; a checkout missing it falls back to compose defaults."""
    path, _ = docker_shim
    home, repo = _home_with_repo(tmp_path)
    r = _run_cli(["up"], home, cwd=home, path=path)
    assert r.returncode == 0, r.stderr
    link = repo / ".env"
    assert link.is_symlink()
    assert link.resolve() == (home / ".agentibrain" / ".env").resolve()


def test_compose_env_drops_variables_the_brain_owns(tmp_path, monkeypatch):
    from agentibrain import bootstrap

    (tmp_path / ".env").write_text("LOG_LEVEL=INFO\n")
    (tmp_path / "compose.yml").write_text("x: ${REDIS_URL:-redis://redis}\ny: $${KEEP_ME}\n")
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("REDIS_URL", "redis://elsewhere")
    monkeypatch.setenv("KEEP_ME", "1")
    env = bootstrap._compose_env(tmp_path)
    assert "LOG_LEVEL" not in env
    assert "REDIS_URL" not in env
    assert env["KEEP_ME"] == "1"


def test_a_stack_command_never_rewrites_an_existing_pin(tmp_path, docker_shim):
    path, _ = docker_shim
    home, pinned = _home_with_repo(tmp_path)
    other = tmp_path / "checkout-b"
    other.mkdir()
    (other / "compose.yml").write_text(MARKER_COMPOSE)
    r = _run_cli(["build"], home, cwd=other, path=path)
    assert r.returncode == 0, r.stderr
    assert (home / ".agentibrain" / ".env").read_text() == f"AGENTIBRAIN_REPO={pinned}\n"
