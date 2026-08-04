"""Root compose.yml contract: transcript extraction wiring + vault location.

Guards the tick-cron extraction path (transcripts mount, env-driven gate,
boot seed) and the ~/agentibrain-vault default shared by every
vault-mounting service. None of this is covered by test_compose_render.py,
which exercises the Jinja template used by `brain init`, not this file.
"""

from pathlib import Path

import yaml

COMPOSE = Path(__file__).resolve().parents[2] / "compose.yml"

VAULT_SERVICES = ("brain-api", "tick-cron", "tick-drain", "amygdala")
VAULT_MOUNT = "${VAULT_ROOT_HOST:-~/agentibrain-vault}:/vault"
PROJECTS_MOUNT = (
    "${CLAUDE_PROJECTS_HOST:-~/.claude/projects}:/shared/.claude/projects:ro"
)


def _services() -> dict:
    return yaml.safe_load(COMPOSE.read_text())["services"]


def test_tick_cron_mounts_claude_projects_read_only():
    volumes = _services()["tick-cron"]["volumes"]
    assert PROJECTS_MOUNT in volumes


def test_vault_defaults_to_home_not_repo():
    services = _services()
    for name in VAULT_SERVICES:
        assert VAULT_MOUNT in services[name]["volumes"], name
    assert "./vault" not in COMPOSE.read_text()


def test_extraction_gate_is_env_driven():
    cmd = _services()["tick-cron"]["command"][0]
    for var in (
        "EXTRACT_HOUR",
        "EXTRACT_SINCE",
        "EXTRACT_MIN_TURNS",
        "EXTRACT_PROJECTS_DIR",
        "EXTRACT_ON_BOOT",
        "EXTRACT_BOOT_SINCE",
    ):
        assert var in cmd, var
    # The pre-env-var regression: hour gate hardcoded to 04.
    assert '"$${HOUR}" = "04"' not in cmd
    # Single-digit EXTRACT_HOUR must be normalised to match `date -u +%H`.
    assert 'GATE_HOUR="0$${GATE_HOUR}"' in cmd
    # Docker auto-creates missing bind sources, so the transcripts guard must
    # test emptiness, not mere existence.
    assert "ls -A" in cmd


def test_extraction_env_defaults_present():
    env = _services()["tick-cron"]["environment"]
    assert env["EXTRACT_HOUR"] == "${EXTRACT_HOUR:-04}"
    assert env["EXTRACT_SINCE"] == "${EXTRACT_SINCE:-26h}"
    assert env["EXTRACT_ON_BOOT"] == "${EXTRACT_ON_BOOT:-0}"
    assert env["EXTRACT_BOOT_SINCE"] == "${EXTRACT_BOOT_SINCE:-90d}"
    assert env["EXTRACT_PROJECTS_DIR"] == "/shared/.claude/projects"
