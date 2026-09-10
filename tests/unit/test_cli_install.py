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

from agentibrain import cli

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


def test_install_forwards_target_and_no_init(monkeypatch):
    captured: dict[str, list[str]] = {}

    class _Done:
        returncode = 0

    def _record(cmd):
        captured["cmd"] = cmd
        return _Done()

    monkeypatch.setattr(cli, "_agentihooks_bin", lambda: "/usr/bin/agentihooks")
    monkeypatch.setattr(cli.subprocess, "run", _record)

    result = CliRunner().invoke(cli.main, ["install", "--for-target", "codex", "--no-init"])

    assert result.exit_code == 0
    assert captured["cmd"][-3:] == ["--for-target", "codex", "--no-init"]


def test_install_exits_when_agentihooks_is_absent(monkeypatch, tmp_path):
    monkeypatch.setattr(cli.sys, "executable", str(tmp_path / "python"))
    monkeypatch.setattr(cli.shutil, "which", lambda _: None)

    result = CliRunner().invoke(cli.main, ["install"])

    assert result.exit_code == 1
    assert "agentihooks not found" in _flat(result.output)


def test_install_exits_when_profile_data_is_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "PROFILES_ROOT", tmp_path / "profiles")

    result = CliRunner().invoke(cli.main, ["install"])

    assert result.exit_code == 1
    assert "is missing" in _flat(result.output)
