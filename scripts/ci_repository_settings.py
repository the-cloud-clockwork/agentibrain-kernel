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
        "dev-no-delete": ("refs/heads/dev", {"deletion", "non_fast_forward"}),
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
            or rule.get("bypass_actors") != []
            or refs.get("include") != [branch]
            or refs.get("exclude") != []
            or not controls <= {control.get("type") for control in rule.get("rules", [])}
        ):
            problems.append(f"{name} protection differs from the approved settings")
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
    for problem in problems:
        print(f"::error::{problem}")
    return int(bool(problems))


if __name__ == "__main__":
    raise SystemExit(main())
