"""Scaffold tests — folder tree + schema marker."""

from __future__ import annotations

import json

from agentibrain.scaffold import SCHEMA_VERSION, VAULT_FOLDERS, SchemaConflict, scaffold


def test_scaffold_creates_folders_and_schema(tmp_path):
    vault = tmp_path / "vault"
    result = scaffold(vault)
    assert result["folders_created"] == len(VAULT_FOLDERS)
    for rel in VAULT_FOLDERS:
        assert (vault / rel).is_dir()
    schema = json.loads((vault / ".brain-schema").read_text())
    assert schema["version"] == SCHEMA_VERSION


def test_scaffold_is_idempotent(tmp_path):
    vault = tmp_path / "vault"
    scaffold(vault)
    result = scaffold(vault)
    assert result["folders_created"] == 0


def test_scaffold_rejects_version_mismatch(tmp_path):
    vault = tmp_path / "vault"
    scaffold(vault)
    # Fake an older schema on disk.
    (vault / ".brain-schema").write_text(json.dumps({"version": "0", "schema": "old"}))
    try:
        scaffold(vault)
    except SchemaConflict:
        pass
    else:
        raise AssertionError("expected SchemaConflict")
    # With force_upgrade, it rewrites.
    result = scaffold(vault, force_upgrade=True)
    assert result["schema"]["version"] == SCHEMA_VERSION


def test_templates_seeded(tmp_path):
    vault = tmp_path / "vault"
    scaffold(vault)
    assert (vault / "templates" / "arc.md").is_file()
    assert (vault / "README.md").is_file()
