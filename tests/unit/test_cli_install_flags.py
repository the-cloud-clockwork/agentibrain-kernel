"""install is the only setup command — the flags init carried reach the stack it renders."""

from __future__ import annotations

from pathlib import Path

import yaml
from click.testing import CliRunner

from agentibrain import cli


def _install(tmp_path: Path, monkeypatch, *args: str):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("AGENTIBRAIN_HOME", str(tmp_path / ".agentibrain"))
    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path / ".agentihooks"))
    for leaked in ("KB_ROUTER_TOKEN", "BRAIN_URL", "BRAIN_HTTP_TOKEN"):
        monkeypatch.delenv(leaked, raising=False)
    monkeypatch.setattr(cli, "_start_stack", lambda settings: None)
    return CliRunner().invoke(cli.main, ["install", "--no-link", *args])


def _assigned(env: Path) -> dict[str, str]:
    return dict(
        line.split("=", 1)
        for line in env.read_text().splitlines()
        if line and not line.startswith("#")
    )


def test_install_creates_the_vault_so_docker_cannot_claim_it_as_root(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    result = _install(tmp_path, monkeypatch, "--vault", str(vault))

    assert result.exit_code == 0, result.output
    assert vault.is_dir()


def test_s3_bucket_renders_an_s3_stack(tmp_path, monkeypatch):
    result = _install(
        tmp_path, monkeypatch, "--s3-bucket", "brain-bucket", "--s3-endpoint", "http://s3.local"
    )

    assert result.exit_code == 0, result.output
    cfg = yaml.safe_load((tmp_path / ".agentibrain" / "config.yaml").read_text())
    assert cfg["mode"] == "s3"
    assert cfg["s3_bucket"] == "brain-bucket"
    assert "agentibrain_minio" not in (tmp_path / ".agentibrain" / "compose.yml").read_text()


def test_inference_flags_fill_only_what_the_env_lacks(tmp_path, monkeypatch):
    env = tmp_path / ".agentibrain" / ".env"
    env.parent.mkdir(parents=True)
    env.write_text("INFERENCE_URL=http://kept\n")

    result = _install(
        tmp_path, monkeypatch, "--llm-gateway-url", "http://typed", "--openai-key", "typed"
    )

    assert result.exit_code == 0, result.output
    assigned = _assigned(env)
    assert assigned["INFERENCE_URL"] == "http://kept"
    assert assigned["LLM_API_KEY"] == "typed"
    assert assigned["INFERENCE_API_KEY"] == "typed"


def test_init_is_gone():
    result = CliRunner().invoke(cli.main, ["init"])

    assert result.exit_code != 0
    assert "No such command" in result.output
