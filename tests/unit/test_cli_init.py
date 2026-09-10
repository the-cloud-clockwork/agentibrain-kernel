"""``agentibrain init`` — what has to exist on disk before the stack starts.

The vault is a bind-mount source. If it does not exist when compose runs,
Docker creates it as root and every later `agentibrain scaffold` dies with
PermissionError, so init owns creating it.
"""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from agentibrain import cli


def _init(tmp_path: Path, monkeypatch, *args: str):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    # AGENTIBRAIN_HOME is what config_dir() reads, and it is read per call.
    # Patching module constants here did nothing: init builds BrainSettings,
    # whose config_dir default was captured at import from the real home.
    monkeypatch.setenv("AGENTIBRAIN_HOME", str(tmp_path / ".agentibrain"))
    return CliRunner().invoke(cli.main, ["init", "--local", *args])


def test_init_creates_the_vault_so_docker_cannot_claim_it_as_root(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    result = _init(tmp_path, monkeypatch, "--vault", str(vault))

    assert result.exit_code == 0, result.output
    assert vault.is_dir()


def test_init_creates_the_default_vault_when_none_is_given(tmp_path, monkeypatch):
    result = _init(tmp_path, monkeypatch)

    assert result.exit_code == 0, result.output
    assert (tmp_path / "agentibrain-vault").is_dir()


def test_init_tells_the_operator_to_scaffold_before_starting_the_stack(tmp_path, monkeypatch):
    result = _init(tmp_path, monkeypatch)

    flat = " ".join(result.output.split())
    assert flat.index("agentibrain scaffold") < flat.index("agentibrain up")
