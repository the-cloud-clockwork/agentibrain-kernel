"""Scaffold tests — folder tree + schema marker."""

from __future__ import annotations

import json

from agentibrain.scaffold import SCHEMA_VERSION, SchemaConflict, scaffold

EXPECTED_DIRS = (
    "amygdala",
    "brain-feed",
    "brain-feed/ticks",
    "bridge",
    "clusters",
    "daily",
    "frontal-lobe",
    "frontal-lobe/conscious",
    "frontal-lobe/unconscious",
    "identity",
    "left",
    "left/decisions",
    "left/incidents",
    "left/projects",
    "left/reference",
    "left/research",
    "pineal",
    "raw",
    "raw/articles",
    "raw/inbox",
    "raw/media",
    "raw/transcripts",
    "right",
    "right/creative",
    "right/ideas",
    "right/life",
    "right/risk",
    "right/strategy",
    "templates",
    "templates/mubs",
)

EXPECTED_FILES = (
    "README.md",
    "CLAUDE.md",
    "bridge/_index.md",
    "bridge/vision.md",
    "bridge/connections.md",
    "bridge/weekly-synthesis.md",
    "identity/README.md",
    "identity/about-me.template.md",
    "identity/goals.template.md",
    "identity/principles.template.md",
    "identity/stack.template.md",
    "left/_index.md",
    "right/_index.md",
    "templates/decision.md",
    "templates/idea.md",
    "templates/incident.md",
    "templates/project.md",
    "templates/research.md",
    "templates/mubs/VISION.md",
    "templates/mubs/SPECS.md",
    "templates/mubs/BLOCKS.md",
    "templates/mubs/TODO.md",
    "templates/mubs/STATE.md",
    "templates/mubs/BUGS.md",
    "templates/mubs/KNOWN-ISSUES.md",
    "templates/mubs/ENHANCEMENTS.md",
    "templates/mubs/MVP.md",
    "templates/mubs/PATCHES.md",
)


def test_scaffold_creates_full_tree(tmp_path):
    vault = tmp_path / "vault"
    result = scaffold(vault)
    assert result["folders_created"] >= len(EXPECTED_DIRS)
    assert result["files_written"] >= len(EXPECTED_FILES)
    for rel in EXPECTED_DIRS:
        assert (vault / rel).is_dir(), f"missing dir: {rel}"
    for rel in EXPECTED_FILES:
        assert (vault / rel).is_file(), f"missing file: {rel}"
    schema = json.loads((vault / ".brain-schema").read_text())
    assert schema["version"] == SCHEMA_VERSION


def test_scaffold_is_idempotent(tmp_path):
    vault = tmp_path / "vault"
    scaffold(vault)
    result = scaffold(vault)
    assert result["folders_created"] == 0
    assert result["files_written"] == 0


def test_scaffold_preserves_operator_edits(tmp_path):
    vault = tmp_path / "vault"
    scaffold(vault)
    about = vault / "identity" / "about-me.template.md"
    about.write_text("# MY EDITED ABOUT-ME\n")
    scaffold(vault)
    assert about.read_text() == "# MY EDITED ABOUT-ME\n", "scaffold clobbered an existing file"


def test_scaffold_rejects_version_mismatch(tmp_path):
    vault = tmp_path / "vault"
    scaffold(vault)
    (vault / ".brain-schema").write_text(json.dumps({"version": "0", "schema": "old"}))
    try:
        scaffold(vault)
    except SchemaConflict:
        pass
    else:
        raise AssertionError("expected SchemaConflict")
    result = scaffold(vault, force_upgrade=True)
    assert result["schema"]["version"] == SCHEMA_VERSION


def test_mubs_templates_all_shipped(tmp_path):
    vault = tmp_path / "vault"
    scaffold(vault)
    mubs = vault / "templates" / "mubs"
    for name in (
        "VISION",
        "SPECS",
        "BLOCKS",
        "TODO",
        "STATE",
        "BUGS",
        "KNOWN-ISSUES",
        "ENHANCEMENTS",
        "MVP",
        "PATCHES",
    ):
        assert (mubs / f"{name}.md").is_file(), f"MUBS template missing: {name}"


def test_claude_md_shipped_at_root(tmp_path):
    vault = tmp_path / "vault"
    scaffold(vault)
    claude = (vault / "CLAUDE.md").read_text()
    assert "Vault Rules for AI Agents" in claude
    assert "MUBS" in claude
