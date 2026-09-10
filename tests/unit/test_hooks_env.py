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

from agentibrain import cli, hooks_env


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


def test_managed_file_carries_every_key_the_hooks_need(hooks_home):
    path = hooks_env.write_hooks_env(brain_url="http://127.0.0.1:8103", token="t0ken")

    assigned = _assignments(path)
    assert assigned["BRAIN_URL"] == "http://127.0.0.1:8103"
    assert assigned["BRAIN_HTTP_TOKEN"] == "t0ken"
    assert assigned["BRAIN_ENABLED"] == "true"
    assert assigned["BRAIN_WRITER_ENABLED"] == "true"
    assert assigned["BRAIN_WRITER_OUTBOX"].endswith("brain-outbox")


def test_managed_file_is_secret_mode(hooks_home):
    path = hooks_env.write_hooks_env(brain_url="http://x", token="t0ken")
    assert path.stat().st_mode & 0o777 == 0o600


def test_rewriting_does_not_accumulate_duplicates(hooks_home):
    hooks_env.write_hooks_env(brain_url="http://first", token="a")
    path = hooks_env.write_hooks_env(brain_url="http://second", token="b")

    keys = [line.split("=", 1)[0] for line in path.read_text().splitlines() if "=" in line]
    assert len(keys) == len(set(keys))
    assert _assignments(path)["BRAIN_URL"] == "http://second"


def test_managed_name_sorts_after_dotenv_and_before_a_zz_companion(hooks_home):
    """Load order is the whole contract: the operator keeps a way to override."""
    assert ".env" < hooks_env.MANAGED_ENV_NAME < "zz-operator.env"


def test_outbox_dirs_are_created_user_owned(hooks_home):
    created = hooks_env.ensure_outbox_dirs()
    assert [d.name for d in created] == ["brain-outbox", "brain-outbox-backlog"]
    assert all(d.is_dir() for d in created)


def test_written_config_reads_back_as_enabled(hooks_home, no_agentihooks):
    hooks_env.write_hooks_env(brain_url="http://127.0.0.1:8103", token="t0ken")

    cfg = hooks_env.resolve_consumer_config()
    assert cfg["brain_url"] == "http://127.0.0.1:8103"
    assert cfg["reader_enabled"] is True
    assert cfg["writer_enabled"] is True
    assert cfg["token_present"] is True


def test_a_later_companion_overrides_the_managed_file(hooks_home, no_agentihooks):
    hooks_env.write_hooks_env(brain_url="http://managed", token="t0ken")
    (hooks_home / "zz-operator.env").write_text("BRAIN_URL=http://operator\n")

    assert hooks_env.resolve_consumer_config()["brain_url"] == "http://operator"


def test_process_env_beats_every_file(hooks_home, no_agentihooks, monkeypatch):
    hooks_env.write_hooks_env(brain_url="http://managed", token="t0ken")
    monkeypatch.setenv("BRAIN_URL", "http://from-process")

    assert hooks_env.resolve_consumer_config()["brain_url"] == "http://from-process"


def test_kb_router_token_is_accepted_as_the_fallback_name(hooks_home, no_agentihooks):
    (hooks_home / ".env").write_text("BRAIN_URL=http://x\nKB_ROUTER_TOKEN=legacy\n")

    assert hooks_env.resolve_consumer_config()["token_present"] is True


def test_a_url_without_a_token_is_reported_as_missing(hooks_home, no_agentihooks):
    (hooks_home / ".env").write_text("BRAIN_URL=http://x\n")

    cfg = hooks_env.resolve_consumer_config()
    assert cfg["token_present"] is False
    assert cfg["reader_enabled"] is True


def test_explicit_false_survives_the_bridge(hooks_home, no_agentihooks):
    hooks_env.write_hooks_env(brain_url="http://x", token="t")
    (hooks_home / "zz-operator.env").write_text("BRAIN_ENABLED=false\n")

    assert hooks_env.resolve_consumer_config()["reader_enabled"] is False


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
    for step in ("1. deployment", "2. vault", "3. stack", "4. agentihooks config", "5. profile"):
        assert step in flat
    assert not hooks_env.managed_env_path().exists()
