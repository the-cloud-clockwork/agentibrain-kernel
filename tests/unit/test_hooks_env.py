"""The bridge between the kernel's config and the one agentihooks reads.

The two resolve from different files — the kernel from ~/.agentibrain/.env,
agentihooks from the ~/.agentihooks env chain — and nothing joined them. A
token written only to the kernel's file authenticates every `agentibrain`
command while every marker POST from the hook answers 401, which is the exact
shape of the failure this module exists to prevent.
"""

from __future__ import annotations

import sys

import pytest
from click.testing import CliRunner

from agentibrain import bootstrap, cli, hooks_env


@pytest.fixture
def hooks_home(tmp_path, monkeypatch):
    home = tmp_path / ".agentihooks"
    home.mkdir()
    monkeypatch.setenv("AGENTIHOOKS_HOME", str(home))
    for key in (
        "BRAIN_URL",
        "BRAIN_ENABLED",
        "BRAIN_WRITER_ENABLED",
        "BRAIN_HTTP_TOKEN",
        "KB_ROUTER_TOKEN",
    ):
        monkeypatch.delenv(key, raising=False)
    return home


@pytest.fixture
def no_agentihooks(monkeypatch):
    """Force the file-chain fallback even though agentihooks is installed here."""
    monkeypatch.setitem(sys.modules, "hooks", None)


def _assignments(path):
    return dict(
        line.split("=", 1)
        for line in path.read_text().splitlines()
        if line and not line.startswith("#")
    )


def test_outbox_dirs_are_created_user_owned(hooks_home):
    created = hooks_env.ensure_outbox_dirs()
    assert [d.name for d in created] == ["brain-outbox", "brain-outbox-backlog"]
    assert all(d.is_dir() for d in created)


# ── the probe that check runs ─────────────────────────────────────────


def test_probe_fails_without_a_url():
    result = cli._probe_hooks_auth({"brain_url": "", "token_present": False})
    assert result["status"] == "fail"
    assert "BRAIN_URL" in result["reason"]


def test_probe_fails_without_a_token():
    result = cli._probe_hooks_auth({"brain_url": "http://x", "token_present": False})
    assert result["status"] == "fail"
    assert "TOKEN" in result["reason"].upper()


def test_probe_fails_on_401(monkeypatch):
    monkeypatch.setattr(cli, "_get_json", lambda *a, **k: (None, "HTTP 401", 401))
    result = cli._probe_hooks_auth({"brain_url": "http://x", "token_present": True, "token": "bad"})
    assert result["status"] == "fail"
    assert result["http"] == 401


def test_probe_passes_when_the_hooks_token_is_accepted(monkeypatch):
    monkeypatch.setattr(cli, "_get_json", lambda *a, **k: ({"entries": []}, None, 200))
    result = cli._probe_hooks_auth(
        {"brain_url": "http://x", "token_present": True, "token": "good"}
    )
    assert result["status"] == "ok"


# ── install ───────────────────────────────────────────────────────────


def test_install_dry_run_changes_nothing(hooks_home, tmp_path, monkeypatch):
    from agentibrain.config import BrainSettings

    monkeypatch.setattr(
        cli, "_load_settings", lambda: BrainSettings(config_dir=tmp_path, _env_file=None)
    )
    monkeypatch.setattr(cli, "_agentihooks_bin", lambda: "/usr/bin/agentihooks")

    result = CliRunner().invoke(cli.main, ["install", "--dry-run", "--no-stack", "--no-link"])

    assert result.exit_code == 0, result.output
    flat = " ".join(result.output.split())
    for step in ("1. deployment", "2. vault", "3. stack", "4. brain config", "5. profile"):
        assert step in flat
    assert not hooks_env.managed_env_path().exists()


# ── client-only: a machine that talks to a brain it does not host ─────


@pytest.fixture
def client_only(hooks_home, tmp_path, monkeypatch):
    """No local deployment, no vault, no keys — only a URL and a bearer."""
    from agentibrain.config import BrainSettings

    monkeypatch.setattr(
        cli, "_load_settings", lambda: BrainSettings(config_dir=tmp_path, _env_file=None)
    )
    monkeypatch.setattr(cli, "_agentihooks_bin", lambda: "/usr/bin/agentihooks")
    touched: dict[str, bool] = {}
    monkeypatch.setattr(
        cli, "_create_deployment", lambda *a, **kw: touched.setdefault("created", True)
    )
    monkeypatch.setattr(cli, "_start_stack", lambda settings: touched.setdefault("started", True))
    monkeypatch.setattr(
        cli._scaffold, "scaffold", lambda path, **kw: touched.setdefault("scaffolded", True)
    )
    return touched


# ── the local case: the machine already knows both values ────────────


@pytest.fixture
def running_checkout(hooks_home, tmp_path, monkeypatch):
    """A repo-checkout deployment whose .env is NOT the one under config_dir."""
    from agentibrain.config import BrainSettings

    repo = tmp_path / "checkout"
    repo.mkdir()
    cfg = tmp_path / "config-dir"
    cfg.mkdir()

    monkeypatch.setattr(
        cli, "_load_settings", lambda: BrainSettings(config_dir=cfg, _env_file=None)
    )
    monkeypatch.setattr(
        cli.bootstrap, "find_deployment", lambda settings, cwd=None: ("root-compose", repo)
    )
    monkeypatch.setattr(
        cli._scaffold,
        "scaffold",
        lambda path, **kw: {"vault": str(path), "folders_created": 0, "files_written": 0},
    )
    monkeypatch.setattr(cli, "_agentihooks_bin", lambda: "/usr/bin/agentihooks")
    return repo


def _managed():
    return hooks_env.managed_env_path().read_text()


# ── one file: the brain's own .env ───────────────────────────────────


def _assignments(path):
    return dict(
        line.split("=", 1)
        for line in path.read_text().splitlines()
        if line and not line.startswith("#")
    )


def test_upsert_adds_only_what_is_missing(tmp_path):
    env = tmp_path / ".env"
    bootstrap.upsert_env_values(env, {"BRAIN_URL": "http://a", "KB_ROUTER_TOKEN": "t1"})
    added = bootstrap.upsert_env_values(env, {"BRAIN_URL": "http://b", "OTHER": "x"})

    assert added == ["OTHER"]
    # A value already in the file is never rewritten — a rotation stays rotated.
    assert _assignments(env)["BRAIN_URL"] == "http://a"
    assert env.stat().st_mode & 0o777 == 0o600


def test_install_completes_the_brain_env_and_copies_nothing(running_checkout):
    (running_checkout / ".env").write_text("KB_ROUTER_TOKEN=from-deployment\n")

    result = CliRunner().invoke(cli.main, ["install", "--no-stack", "--no-link"])

    assert result.exit_code == 0, result.output
    assigned = _assignments(running_checkout / ".env")
    assert assigned["KB_ROUTER_TOKEN"] == "from-deployment"
    assert assigned["BRAIN_URL"].startswith("http://")
    assert not hooks_env.managed_env_path().exists()


def test_a_remote_install_writes_the_client_its_own_brain_env(client_only, tmp_path):
    CliRunner().invoke(
        cli.main,
        ["install", "--no-link", "--brain-url", "http://central:8103", "--token", "remote-t"],
    )

    assigned = _assignments(tmp_path / ".env")
    assert assigned["BRAIN_URL"] == "http://central:8103"
    assert assigned["KB_ROUTER_TOKEN"] == "remote-t"
    assert client_only == {}


def test_the_stale_projected_copy_is_swept(running_checkout):
    (running_checkout / ".env").write_text("KB_ROUTER_TOKEN=t\n")
    stale = hooks_env.managed_env_path()
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_text("# Managed by `agentibrain install` — old copy\nBRAIN_URL=http://stale\n")

    CliRunner().invoke(cli.main, ["install", "--no-stack", "--no-link"])

    assert not stale.exists()


def test_a_file_the_operator_wrote_is_not_swept(running_checkout):
    (running_checkout / ".env").write_text("KB_ROUTER_TOKEN=t\n")
    mine = hooks_env.managed_env_path()
    mine.parent.mkdir(parents=True, exist_ok=True)
    mine.write_text("BRAIN_REFRESH_INTERVAL=5\n")

    CliRunner().invoke(cli.main, ["install", "--no-stack", "--no-link"])

    assert mine.exists()
