import re
from pathlib import Path

import yaml

WORKFLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"


def test_release_uses_scoped_app_credentials_without_personal_tokens():
    personal_tokens = {
        "ORG_GITHUB_TOKEN",
        "GHCR_PAT",
        "UPSTREAM_SYNC_PAT",
        "RELEASE_TOKEN",
        "QITP_ANTON_DISPATCH_TOKEN",
    }
    for workflow in WORKFLOWS.glob("*.y*ml"):
        reads = set(re.findall(r"secrets\.([A-Za-z_][A-Za-z0-9_]*)", workflow.read_text()))
        assert not reads & personal_tokens, workflow.name

    release = yaml.safe_load((WORKFLOWS / "release.yml").read_text())
    steps = release["jobs"]["release"]["steps"]
    app = next(
        step
        for step in steps
        if step.get("uses", "").startswith("actions/create-github-app-token@")
    )
    assert app["with"] == {
        "app-id": "${{ secrets.TCC_CI_APP_ID }}",
        "private-key": "${{ secrets.TCC_CI_APP_PRIVATE_KEY }}",
        "owner": "${{ github.repository_owner }}",
        "repositories": "${{ github.event.repository.name }}",
        "permission-contents": "write",
        "permission-actions": "write",
    }
    token = "${{ steps." + app["id"] + ".outputs.token }}"
    checkout = next(step for step in steps if step.get("uses", "").startswith("actions/checkout@"))
    assert steps.index(app) < steps.index(checkout)
    assert checkout["with"]["token"] == token
    api_steps = [step for step in steps if "gh " in step.get("run", "")]
    assert len(api_steps) == 2
    assert all(step["env"]["GH_TOKEN"] == token for step in api_steps)

    registry = yaml.safe_load((WORKFLOWS / "docker-build.yml").read_text())
    build = registry["jobs"]["build"]
    login = next(
        step for step in build["steps"] if step.get("uses", "").startswith("docker/login-action@")
    )
    assert login["with"]["password"] == "${{ secrets.GITHUB_TOKEN }}"
    assert registry["permissions"]["packages"] == "write"
