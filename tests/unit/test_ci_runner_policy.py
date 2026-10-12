import json
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github/workflows"
SELECTOR = (
    "${{ (github.event_name != 'pull_request_target' && "
    "(github.event_name != 'pull_request' || github.event.pull_request.head.repo.full_name == github.repository) && "
    'contains(fromJSON(\'["nestorcolt","agenticore-app[bot]","tcc-main-ci[bot]"]\'), github.actor) && '
    'contains(fromJSON(\'["nestorcolt","agenticore-app[bot]","tcc-main-ci[bot]"]\'), github.triggering_actor) || '
    "(github.event_name == 'merge_group' || github.event_name == 'push' && github.ref == 'refs/heads/dev') && "
    "github.actor == 'github-merge-queue[bot]' && github.triggering_actor == 'github-merge-queue[bot]') && "
    "'anton-oss' || 'ubuntu-latest' }}"
)


@pytest.mark.parametrize(
    ("workflow", "job", "pool"),
    [
        ("ci.yml", "lint", "anton-oss-light"),
        ("deploy-assets.yml", "validate", "anton-oss-light"),
        ("runner-security.yml", "runner-policy", "anton-oss-light"),
        ("runner-security.yml", "settings-audit", "anton-oss-light"),
    ],
)
def test_linux_checks_use_the_protected_trusted_selector(workflow, job, pool):
    data = yaml.safe_load((WORKFLOWS / workflow).read_text())
    assert data["jobs"][job]["runs-on"] == SELECTOR.replace("'anton-oss'", f"'{pool}'")


@pytest.mark.parametrize(
    ("workflow", "job", "runner"),
    [
        ("ci.yml", "lint-test", "ubuntu-latest"),
        ("docker-build.yml", "build", "ubuntu-latest"),
        ("ci.yml", "macos-smoke", "macos-latest"),
        ("pages.yml", "build", "ubuntu-latest"),
        ("pages.yml", "deploy", "ubuntu-latest"),
        ("publish-pypi.yml", "publish", "ubuntu-latest"),
        ("release.yml", "release", "ubuntu-latest"),
    ],
)
def test_hosted_exemptions_remain_hosted(workflow, job, runner):
    data = yaml.safe_load((WORKFLOWS / workflow).read_text())
    assert data["jobs"][job]["runs-on"] == runner


def test_trusted_actor_policy_matches_the_shared_approvals():
    assert json.loads((WORKFLOWS / "trusted-actors.json").read_text()) == {
        "actors": ["nestorcolt", "agenticore-app[bot]", "tcc-main-ci[bot]"],
        "queue_actor": "github-merge-queue[bot]",
    }
