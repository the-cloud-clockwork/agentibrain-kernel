import importlib.util
import json
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/ci_repository_settings.py"
SPEC = importlib.util.spec_from_file_location("ci_repository_settings", SCRIPT)
settings = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(settings)


@pytest.fixture
def snapshot():
    return {
        "contributors": {"approval_policy": "all_external_contributors"},
        "workflow": {
            "default_workflow_permissions": "read",
            "can_approve_pull_request_reviews": False,
        },
        "rulesets": [
            json.loads((ROOT / ".github/rulesets/dev-no-delete.json").read_text()),
            {
                "name": "main-prod-lockdown",
                "enforcement": "active",
                "bypass_actors": [],
                "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
                "rules": [
                    {"type": "deletion"},
                    {"type": "non_fast_forward"},
                    {"type": "required_linear_history"},
                    {"type": "pull_request"},
                ],
            },
        ],
    }


def test_current_security_settings_pass(snapshot):
    assert settings.check(snapshot) == []


def test_read_only_ruleset_responses_omit_bypass_inventory(snapshot):
    for rule in snapshot["rulesets"]:
        rule.pop("bypass_actors")
    assert settings.check(snapshot) == []


@pytest.mark.parametrize(
    ("section", "key", "value", "message"),
    [
        ("contributors", "approval_policy", "first_time_contributors", "external contributors"),
        ("workflow", "default_workflow_permissions", "write", "read only"),
        ("workflow", "can_approve_pull_request_reviews", True, "approve reviews"),
    ],
)
def test_security_setting_drift_fails(snapshot, section, key, value, message):
    snapshot[section][key] = value
    assert any(message in problem for problem in settings.check(snapshot))


@pytest.mark.parametrize("index", [0, 1])
@pytest.mark.parametrize(
    "change", ["disabled", "bypass", "missing_rule", "wrong_branch", "excluded"]
)
def test_branch_protection_drift_fails(snapshot, index, change):
    rule = snapshot["rulesets"][index]
    if change == "disabled":
        rule["enforcement"] = "disabled"
    elif change == "bypass":
        rule["bypass_actors"] = [{"actor_id": 1, "actor_type": "Integration"}]
    elif change == "missing_rule":
        rule["rules"].pop()
    elif change == "wrong_branch":
        rule["conditions"]["ref_name"]["include"] = ["refs/heads/other"]
    else:
        rule["conditions"]["ref_name"]["exclude"] = ["refs/heads/main"]
    assert settings.check(snapshot)


@pytest.mark.parametrize(
    "control",
    [
        "deletion",
        "non_fast_forward",
        "required_linear_history",
        "pull_request",
        "required_status_checks",
        "merge_queue",
    ],
)
def test_missing_dev_control_fails(snapshot, control):
    dev = snapshot["rulesets"][0]
    dev["rules"] = [rule for rule in dev["rules"] if rule["type"] != control]
    assert settings.check(snapshot)


@pytest.mark.parametrize(
    ("control", "parameter", "value"),
    [
        ("pull_request", "allowed_merge_methods", ["merge", "squash"]),
        ("pull_request", "required_approving_review_count", 1),
        ("required_status_checks", "required_status_checks", []),
        (
            "required_status_checks",
            "required_status_checks",
            [{"context": "Other check", "integration_id": 15368}],
        ),
        (
            "required_status_checks",
            "required_status_checks",
            [{"context": "Gate — Required", "integration_id": 1}],
        ),
        ("required_status_checks", "do_not_enforce_on_create", True),
        ("merge_queue", "merge_method", "MERGE"),
        ("merge_queue", "grouping_strategy", "HEADGREEN"),
    ],
)
def test_dev_rule_parameters_drift_fails(snapshot, control, parameter, value):
    rule = next(rule for rule in snapshot["rulesets"][0]["rules"] if rule["type"] == control)
    rule["parameters"][parameter] = value
    assert settings.check(snapshot)


def test_missing_readback_fails():
    assert settings.check({})


def test_owner_push_rule_is_an_explicit_operator_exception(snapshot):
    snapshot["rulesets"].append({"name": "owner-and-apps-only-push", "enforcement": "disabled"})
    assert settings.check(snapshot) == []


def test_cli_reports_all_drift_and_exits_red(snapshot, tmp_path):
    drift = deepcopy(snapshot)
    drift["contributors"]["approval_policy"] = "first_time_contributors"
    drift["workflow"]["can_approve_pull_request_reviews"] = True
    source = tmp_path / "settings.json"
    source.write_text(json.dumps(drift))
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--snapshot", str(source)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "2 problems" in result.stdout
    assert "external contributors" in result.stdout
    assert "approve reviews" in result.stdout
