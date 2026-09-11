"""agentibrain install — the packaged brain profile and the agentihooks handoff.

The profile is data inside the ``agentibrain`` package so a PyPI wheel links
the same overlay a source checkout does. Two things break that silently: the
``.claude/`` overlay falling out of the distribution (setuptools expands
package-data with stdlib glob, whose wildcards skip hidden names — MANIFEST.in
is what carries it), and a path resolved from the repo rather than from the
installed package. The build itself is asserted in tests/integration.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from agentibrain import cli, hooks_env

PROFILE_FILES = (
    "profile.yml",
    "CLAUDE.md",
    ".claude/.mcp.json",
)


def _flat(output: str) -> str:
    return " ".join(output.split())


def test_brain_profile_ships_inside_the_package():
    profile_dir = cli.PROFILES_ROOT / "brain"
    for rel in PROFILE_FILES:
        assert (profile_dir / rel).is_file(), rel


def test_manifest_carries_the_whole_profiles_tree():
    manifest = Path(cli.__file__).parents[1] / "MANIFEST.in"
    if not manifest.is_file():
        pytest.skip("not a source checkout")

    directives = {line.strip() for line in manifest.read_text().splitlines()}
    assert "recursive-include agentibrain/profiles *" in directives


def test_install_dry_run_hands_agentihooks_the_packaged_path(monkeypatch):
    monkeypatch.setattr(cli, "_agentihooks_bin", lambda: "/usr/bin/agentihooks")
    result = CliRunner().invoke(cli.main, ["install", "--dry-run"])

    assert result.exit_code == 0
    flat = _flat(result.output)
    assert "link-profile link" in flat
    assert str(cli.PROFILES_ROOT / "brain") in flat
    assert "--name brain" in flat


@pytest.fixture
def install_env(tmp_path, monkeypatch):
    """install now drives a whole machine — isolate every side effect."""
    from agentibrain.config import BrainSettings

    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path / ".agentihooks"))
    # install's --token/--brain-url carry envvars, so an unscrubbed environment
    # lets the operator's real values override every stub below.
    for leaked in ("KB_ROUTER_TOKEN", "BRAIN_URL", "BRAIN_HTTP_TOKEN"):
        monkeypatch.delenv(leaked, raising=False)
    monkeypatch.setattr(
        cli, "_load_settings", lambda: BrainSettings(config_dir=tmp_path, _env_file=None)
    )
    monkeypatch.setattr(cli, "_token_from_env_file", lambda settings: "t0ken")
    monkeypatch.setattr(cli.bootstrap, "find_deployment", lambda settings, cwd=None: None)
    monkeypatch.setattr(
        cli._scaffold,
        "scaffold",
        lambda path, **kw: {"vault": str(path), "folders_created": 0, "files_written": 0},
    )
    return tmp_path


def test_install_forwards_target_and_no_init(install_env, monkeypatch):
    captured: dict[str, list[str]] = {}

    class _Done:
        returncode = 0

    def _record(cmd):
        captured["cmd"] = cmd
        return _Done()

    monkeypatch.setattr(cli, "_agentihooks_bin", lambda: "/usr/bin/agentihooks")
    monkeypatch.setattr(cli.subprocess, "run", _record)

    result = CliRunner().invoke(
        cli.main, ["install", "--no-stack", "--for-target", "codex", "--no-init"]
    )

    assert result.exit_code == 0, result.output
    assert captured["cmd"][-3:] == ["--for-target", "codex", "--no-init"]


def test_install_completes_the_brain_env(install_env, monkeypatch):
    """The bearer gets exactly one home — the brain's own file, which
    agentihooks reads directly. Nothing is projected into ~/.agentihooks."""
    monkeypatch.setattr(cli, "_agentihooks_bin", lambda: "/usr/bin/agentihooks")

    result = CliRunner().invoke(cli.main, ["install", "--no-stack", "--no-link"])

    assert result.exit_code == 0, result.output
    assigned = dict(
        line.split("=", 1)
        for line in (install_env / ".env").read_text().splitlines()
        if line and not line.startswith("#")
    )
    assert assigned["KB_ROUTER_TOKEN"] == "t0ken"
    assert assigned["BRAIN_URL"].startswith("http://")
    feed = cli._load_settings().vault_path / "brain-feed"
    assert assigned["BRAIN_SOURCE_PATH"] == str(feed)
    assert assigned["AMYGDALA_SIGNAL_PATH"] == str(feed / "amygdala-active.md")
    assert assigned["BRAIN_WRITER_OUTBOX"] == str(install_env / ".agentihooks" / "brain-outbox")
    for key in ("BRAIN_ENABLED", "AMYGDALA_ENABLED", "BRAIN_WRITER_ENABLED"):
        assert assigned[key] == "true"
    assert assigned["BRAIN_WRITER_MAX_MARKERS"] == "5"
    assert not hooks_env.managed_env_path().exists()


def test_install_keeps_brain_settings_already_in_the_env(install_env, monkeypatch):
    monkeypatch.setattr(cli, "_agentihooks_bin", lambda: "/usr/bin/agentihooks")
    (install_env / ".env").write_text("BRAIN_WRITER_MAX_MARKERS=9\nAMYGDALA_ENABLED=false\n")

    result = CliRunner().invoke(cli.main, ["install", "--no-stack", "--no-link"])

    assert result.exit_code == 0, result.output
    body = (install_env / ".env").read_text()
    assert "BRAIN_WRITER_MAX_MARKERS=9\n" in body
    assert "BRAIN_WRITER_MAX_MARKERS=5" not in body
    assert "AMYGDALA_ENABLED=false\n" in body
    assert "AMYGDALA_ENABLED=true" not in body


_STACK_KEYS = (
    "EMBEDDINGS_API_KEY",
    "EMBEDDINGS_API_KEYS",
    "POSTGRES_PASSWORD",
    "LOG_LEVEL",
    "MINIO_ROOT_USER",
    "MINIO_ROOT_PASSWORD",
    "LLM_EMBED_MODEL",
    "BRAIN_CLASSIFY_MODEL",
    "BRAIN_BRIEF_MODEL",
    "TICK_INTERVAL_SECONDS",
    "TICK_DRAIN_INTERVAL_SECONDS",
    "BRAIN_LLM_TIMEOUT_SECONDS",
    "PORT_POSTGRES",
)


def _assigned(env):
    return dict(
        line.split("=", 1)
        for line in env.read_text().splitlines()
        if line and not line.startswith("#")
    )


def test_install_writes_every_stack_key_but_inference(install_env, monkeypatch):
    monkeypatch.setattr(cli, "_agentihooks_bin", lambda: "/usr/bin/agentihooks")

    result = CliRunner().invoke(cli.main, ["install", "--no-stack", "--no-link"])

    assert result.exit_code == 0, result.output
    assigned = _assigned(install_env / ".env")
    for key in _STACK_KEYS:
        assert assigned.get(key), key
    assert assigned["EMBEDDINGS_API_KEY"] == assigned["EMBEDDINGS_API_KEYS"]
    for key in ("LLM_API_KEY", "LLM_API_BASE", "INFERENCE_URL", "INFERENCE_API_KEY"):
        assert key not in assigned
    assert "INFERENCE_API_KEY" in _flat(result.output)


def test_reinstall_only_adds_and_never_backs_up(install_env, monkeypatch):
    monkeypatch.setattr(cli, "_agentihooks_bin", lambda: "/usr/bin/agentihooks")
    env = install_env / ".env"
    mine = "# mine\nLOG_LEVEL=DEBUG\nEMBED_DIM=\nLLM_API_BASE=http://gateway\n"
    env.write_text(mine)

    result = CliRunner().invoke(cli.main, ["install", "--no-stack", "--no-link"])
    assert result.exit_code == 0, result.output
    first = (env.read_bytes(), env.stat().st_mtime_ns)
    result = CliRunner().invoke(cli.main, ["install", "--no-stack", "--no-link"])
    assert result.exit_code == 0, result.output
    body = env.read_text()

    assert (env.read_bytes(), env.stat().st_mtime_ns) == first
    assert body.startswith(mine)
    keys = [
        line.split("=", 1)[0] for line in body.splitlines() if line and not line.startswith("#")
    ]
    assert len(keys) == len(set(keys))
    assert [p.name for p in install_env.iterdir() if p.name.startswith(".env")] == [".env"]


def test_install_takes_stack_defaults_from_the_compose_file(install_env, monkeypatch):
    monkeypatch.setattr(cli, "_agentihooks_bin", lambda: "/usr/bin/agentihooks")
    checkout = install_env / "checkout"
    checkout.mkdir()
    (checkout / "compose.yml").write_text(
        "services:\n  api:\n    environment:\n      LOG_LEVEL: ${LOG_LEVEL:-WARNING}\n"
        '    ports:\n      - "127.0.0.1:${PORT_POSTGRES:-5439}:5432"\n'
    )
    monkeypatch.setattr(
        cli.bootstrap, "find_deployment", lambda settings, cwd=None: ("root-compose", checkout)
    )

    result = CliRunner().invoke(cli.main, ["install", "--no-stack", "--no-link"])

    assert result.exit_code == 0, result.output
    assert (checkout / ".env").is_symlink()
    assigned = _assigned(install_env / ".env")
    assert assigned["LOG_LEVEL"] == "WARNING"
    assert assigned["PORT_POSTGRES"] == "5439"
    assert assigned["AGENTIBRAIN_REPO"] == str(checkout)


def test_install_exits_when_agentihooks_is_absent(install_env, monkeypatch, tmp_path):
    monkeypatch.setattr(cli.sys, "executable", str(tmp_path / "python"))
    monkeypatch.setattr(cli.shutil, "which", lambda _: None)

    result = CliRunner().invoke(cli.main, ["install", "--no-stack"])

    assert result.exit_code == 1
    assert "agentihooks not found" in _flat(result.output)


def test_install_exits_when_profile_data_is_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "PROFILES_ROOT", tmp_path / "profiles")

    result = CliRunner().invoke(cli.main, ["install"])

    assert result.exit_code == 1
    assert "is missing" in _flat(result.output)
