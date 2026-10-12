import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def _dev_ruleset():
    return json.loads((ROOT / ".github/rulesets/dev-no-delete.json").read_text())


def _ci():
    return yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())


def test_dev_requires_pull_requests_without_bypass():
    ruleset = _dev_ruleset()

    assert ruleset["name"] == "dev-no-delete"
    assert ruleset["target"] == "branch"
    assert ruleset["enforcement"] == "active"
    assert ruleset["bypass_actors"] == []
    assert ruleset["conditions"] == {"ref_name": {"include": ["refs/heads/dev"], "exclude": []}}
    rules = {rule["type"]: rule for rule in ruleset["rules"]}
    assert set(rules) == {
        "deletion",
        "non_fast_forward",
        "required_linear_history",
        "pull_request",
        "required_status_checks",
        "merge_queue",
    }
    assert rules["pull_request"]["parameters"]["required_approving_review_count"] == 0
    assert rules["pull_request"]["parameters"]["allowed_merge_methods"] == ["squash"]
    assert rules["pull_request"]["parameters"]["require_code_owner_review"] is False


def test_dev_requires_the_ci_gate_and_merges_through_the_queue():
    rules = {rule["type"]: rule for rule in _dev_ruleset()["rules"]}
    ci = _ci()

    params = rules["required_status_checks"]["parameters"]
    assert params["strict_required_status_checks_policy"] is False
    assert params["do_not_enforce_on_create"] is False
    assert params["required_status_checks"] == [
        {"context": "Gate — Required", "integration_id": 15368}
    ]
    assert ci["jobs"]["gate-required"]["name"] == "Gate — Required"
    assert rules["merge_queue"]["parameters"]["merge_method"] == "SQUASH"
    assert rules["merge_queue"]["parameters"]["grouping_strategy"] == "ALLGREEN"
    assert ci[True]["merge_group"] == {"types": ["checks_requested"]}


def test_ci_gate_fans_in_every_job_and_runs_even_when_one_fails():
    jobs = _ci()["jobs"]
    gate = jobs["gate-required"]

    assert set(gate["needs"]) == set(jobs) - {"gate-required"}
    assert gate["if"] == "always()"
    assert [name for name, job in jobs.items() if "if" in job and name != "gate-required"] == [
        "macos-smoke"
    ]
