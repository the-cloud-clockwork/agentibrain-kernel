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
    assert {"kb-router", "embeddings", "postgres", "redis"} <= set(
        data["services"]
    )


def test_render_s3_excludes_minio(tmp_path):
    rendered = render_compose(_settings("s3", tmp_path / "v"))
    data = yaml.safe_load(rendered)
    assert "minio" not in data["services"]
    assert "minio-init" not in data["services"]
    assert {"kb-router", "embeddings", "postgres", "redis"} <= set(
        data["services"]
    )


def test_render_mounts_vault(tmp_path):
    vault = tmp_path / "vault"
    rendered = render_compose(_settings("local", vault))
    data = yaml.safe_load(rendered)
    mounts = data["services"]["kb-router"]["volumes"]
    assert any(str(vault.resolve()) in m for m in mounts)
