"""Compose render tests — both S3 and local modes produce valid YAML."""

from __future__ import annotations

from pathlib import Path

import yaml

from agentibrain.bootstrap import render_compose
from agentibrain.config import BrainSettings


def _settings(mode: str, vault: Path) -> BrainSettings:
    return BrainSettings(
        mode=mode,
        vault_path=vault,
        s3_bucket="test-bucket" if mode == "s3" else None,
        _env_file=None,
    )


def test_render_local_includes_minio(tmp_path):
    rendered = render_compose(_settings("local", tmp_path / "v"))
    data = yaml.safe_load(rendered)
    assert "minio" in data["services"]
    assert "minio-init" in data["services"]
    assert {"brain-api", "embeddings", "postgres", "redis"} <= set(data["services"])


def test_render_s3_excludes_minio(tmp_path):
    rendered = render_compose(_settings("s3", tmp_path / "v"))
    data = yaml.safe_load(rendered)
    assert "minio" not in data["services"]
    assert "minio-init" not in data["services"]
    assert {"brain-api", "embeddings", "postgres", "redis"} <= set(data["services"])


# The packaged template renders the whole brain, not just its storage half: a
# deployment without tick-drain never processes what it ingests, and one
# without mcp is unreachable from Claude Code.
_FULL_STACK = {
    "brain-api",
    "embeddings",
    "postgres",
    "redis",
    "tick-cron",
    "tick-drain",
    "amygdala",
    "mcp",
}


def test_render_local_is_the_whole_brain(tmp_path):
    data = yaml.safe_load(render_compose(_settings("local", tmp_path / "v")))
    assert _FULL_STACK <= set(data["services"])


def test_render_s3_is_the_whole_brain(tmp_path):
    data = yaml.safe_load(render_compose(_settings("s3", tmp_path / "v")))
    assert _FULL_STACK <= set(data["services"])


def test_every_first_party_image_uses_the_tag_ci_publishes(tmp_path):
    """CI publishes :dev only — :latest has never existed in the registry."""
    data = yaml.safe_load(render_compose(_settings("local", tmp_path / "v")))
    first_party = [
        s["image"] for s in data["services"].values() if "agentibrain-" in s.get("image", "")
    ]
    assert first_party
    assert all(i.endswith(":dev") for i in first_party), first_party


def test_embeddings_gets_the_variables_the_service_actually_reads(tmp_path):
    """The service reads LLM_API_KEY / LLM_API_BASE. OPENAI_API_KEY is read by
    nothing in the stack, so passing it leaves semantic search dead."""
    data = yaml.safe_load(render_compose(_settings("local", tmp_path / "v")))
    env = data["services"]["embeddings"]["environment"]
    assert {"LLM_API_KEY", "LLM_API_BASE", "LLM_EMBED_MODEL"} <= set(env)
    assert "OPENAI_API_KEY" not in env


def test_brain_api_can_authenticate_to_embeddings(tmp_path):
    data = yaml.safe_load(render_compose(_settings("local", tmp_path / "v")))
    env = data["services"]["brain-api"]["environment"]
    assert "EMBEDDINGS_API_KEY" in env
    assert "OPENAI_API_KEY" not in env


def test_ollama_mode_needs_no_api_key_anywhere(tmp_path):
    s = BrainSettings(mode="local", vault_path=tmp_path / "v", ollama=True, _env_file=None)
    data = yaml.safe_load(render_compose(s))
    assert {"ollama", "ollama-init"} <= set(data["services"])

    emb = data["services"]["embeddings"]["environment"]
    assert "ollama:11434" in emb["LLM_API_BASE"]
    # nomic-embed-text is not a family the service recognises, so an unpinned
    # dimension makes it refuse to start.
    assert "768" in emb["EMBED_DIM"]

    # tick-drain was added after local/compose.ollama.yml was written; leaving
    # it out makes on-demand ticks run --no-ai while scheduled ticks use AI.
    for name in ("brain-api", "mcp", "tick-cron", "tick-drain"):
        assert "ollama:11434" in data["services"][name]["environment"]["INFERENCE_URL"], name


def test_the_chat_model_is_overridable_by_env(tmp_path, monkeypatch):
    """Settings carry the BRAIN_ prefix; a bare OLLAMA_CHAT_MODEL is ignored."""
    monkeypatch.setenv("BRAIN_OLLAMA_CHAT_MODEL", "qwen2.5:14b")
    s = BrainSettings(mode="local", vault_path=tmp_path / "v", ollama=True, _env_file=None)
    data = yaml.safe_load(render_compose(s))
    assert "qwen2.5:14b" in data["services"]["ollama-init"]["command"][0]


def test_without_ollama_nothing_points_at_a_local_model(tmp_path):
    data = yaml.safe_load(render_compose(_settings("local", tmp_path / "v")))
    assert "ollama" not in data["services"]
    assert "ollama" not in data["services"]["embeddings"]["environment"]["LLM_API_BASE"]


def test_the_vault_reaches_every_service_that_writes_it(tmp_path):
    vault = tmp_path / "vault"
    data = yaml.safe_load(render_compose(_settings("local", vault)))
    for name in ("brain-api", "tick-cron", "tick-drain", "amygdala"):
        mounts = data["services"][name]["volumes"]
        assert any(str(vault.resolve()) in m for m in mounts), name


def test_render_mounts_vault(tmp_path):
    vault = tmp_path / "vault"
    rendered = render_compose(_settings("local", vault))
    data = yaml.safe_load(rendered)
    mounts = data["services"]["brain-api"]["volumes"]
    assert any(str(vault.resolve()) in m for m in mounts)
