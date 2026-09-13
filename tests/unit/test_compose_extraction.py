"""Root compose.yml contract: transcript extraction wiring + vault location.

Guards the tick-cron extraction path (transcripts mount, env-driven gate,
boot seed) and the ~/agentibrain-vault default shared by every
vault-mounting service. None of this is covered by test_compose_render.py,
which exercises the Jinja template used by `agentibrain install`, not this file.
"""

from pathlib import Path

import yaml

COMPOSE = Path(__file__).resolve().parents[2] / "compose.yml"

VAULT_SERVICES = ("brain-api", "tick-cron", "tick-drain", "amygdala")
VAULT_MOUNT = "${VAULT_ROOT_HOST:-~/agentibrain-vault}:/vault"
PROJECTS_MOUNT = "${CLAUDE_PROJECTS_HOST:-~/.claude/projects}:/shared/.claude/projects:ro"


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


SYNC_MOUNT = "${AGENTIHOOKS_HOME_HOST:-~/.agentihooks}:/agentihooks"


def test_tick_cron_mounts_agentihooks_buffers():
    assert SYNC_MOUNT in _services()["tick-cron"]["volumes"]


def test_tick_cron_drains_outbox_each_pass():
    svc = _services()["tick-cron"]
    cmd = svc["command"][0]
    assert "outbox_drain.py --outbox /agentihooks/brain-outbox" in cmd
    assert svc["environment"]["BRAIN_API_URL"] == "http://brain-api:8080"
    assert "KB_ROUTER_TOKEN" in svc["environment"]


def test_raw_index_refreshes_in_both_tick_paths():
    services = _services()
    for name in ("tick-cron", "tick-drain"):
        assert "embed_raw.py --vault /vault --prune" in services[name]["command"][0], name


def test_tick_drain_annotates_failed_requests():
    cmd = _services()["tick-drain"]["command"][0]
    assert "annotate_fail.py" in cmd
    assert '> "$${TLOG}" 2>&1' in cmd or '> "$$TLOG" 2>&1' in cmd
