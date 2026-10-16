import argparse
import json
import subprocess
from pathlib import Path


def check(snapshot: dict) -> list[str]:
    problems = []
    if snapshot.get("contributors", {}).get("approval_policy") != "all_external_contributors":
        problems.append("all external contributors must require approval")
    workflow = snapshot.get("workflow", {})
    if workflow.get("default_workflow_permissions") != "read":
        problems.append("workflow tokens must be read only")
    if workflow.get("can_approve_pull_request_reviews") is not False:
        problems.append("workflow tokens must not approve reviews")
    required = {
        "dev-no-delete": (
            "refs/heads/dev",
            {
                "deletion",
                "non_fast_forward",
                "required_linear_history",
                "pull_request",
                "required_status_checks",
                "merge_queue",
            },
        ),
        "main-prod-lockdown": (
            "~DEFAULT_BRANCH",
            {"deletion", "non_fast_forward", "required_linear_history", "pull_request"},
        ),
    }
    for name, (branch, controls) in required.items():
        matches = [rule for rule in snapshot.get("rulesets", []) if rule.get("name") == name]
        if len(matches) != 1:
            problems.append(f"{name} must exist exactly once")
            continue
        rule = matches[0]
        refs = rule.get("conditions", {}).get("ref_name", {})
        if (
            rule.get("enforcement") != "active"
            or rule.get("bypass_actors", []) != []
            or refs.get("include") != [branch]
            or refs.get("exclude") != []
            or not controls <= {control.get("type") for control in rule.get("rules", [])}
        ):
            problems.append(f"{name} protection differs from the approved settings")
        if name == "dev-no-delete":
            parameters = {
                "pull_request": {
                    "allowed_merge_methods": ["squash"],
                    "required_approving_review_count": 0,
                    "dismiss_stale_reviews_on_push": False,
                    "require_code_owner_review": False,
                    "require_last_push_approval": False,
                    "required_review_thread_resolution": False,
                },
                "required_status_checks": {
                    "required_status_checks": [
                        {"context": "Gate — Required", "integration_id": 15368}
                    ],
                    "strict_required_status_checks_policy": False,
                    "do_not_enforce_on_create": False,
                },
                "merge_queue": {
                    "check_response_timeout_minutes": 60,
                    "grouping_strategy": "ALLGREEN",
                    "max_entries_to_build": 5,
                    "max_entries_to_merge": 5,
                    "merge_method": "SQUASH",
                    "min_entries_to_merge": 1,
                    "min_entries_to_merge_wait_minutes": 0,
                },
            }
            for control in rule.get("rules", []):
                expected = parameters.get(control.get("type"), {})
                actual = control.get("parameters", {})
                if any(actual.get(key) != value for key, value in expected.items()):
                    problems.append(
                        f"{name} {control['type']} parameters differ from the approved settings"
                    )
    return problems


def read_repository(repository: str) -> dict:
    def api(endpoint):
        result = subprocess.run(
            ["gh", "api", f"repos/{repository}/{endpoint}"],
            check=True,
            capture_output=True,
            text=True,
        )
        return json.loads(result.stdout)

    inventory = subprocess.run(
        ["gh", "api", "--paginate", "--slurp", f"repos/{repository}/rulesets?per_page=100"],
        check=True,
        capture_output=True,
        text=True,
    )
    rules = [rule for page in json.loads(inventory.stdout) for rule in page]
    return {
        "contributors": api("actions/permissions/fork-pr-contributor-approval"),
        "workflow": api("actions/permissions/workflow"),
        "rulesets": [api(f"rulesets/{rule['id']}") for rule in rules],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--repository")
    source.add_argument("--snapshot", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    snapshot = (
        json.loads(args.snapshot.read_text()) if args.snapshot else read_repository(args.repository)
    )
    if args.report:
        args.report.write_text(json.dumps(snapshot, indent=2) + "\n")
    problems = check(snapshot)
    print(f"Repository security: {len(problems)} problems")
    if any("bypass_actors" not in rule for rule in snapshot.get("rulesets", [])):
        print("::notice::Bypass actor inventory is unproven with read only credentials")
    for problem in problems:
        print(f"::error::{problem}")
    return int(bool(problems))


if __name__ == "__main__":
    raise SystemExit(main())
