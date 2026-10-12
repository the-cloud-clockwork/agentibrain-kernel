from pathlib import Path

import pytest
import yaml

WORKFLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"

TRUSTED = (
    "(github.event_name != 'pull_request_target'"
    " && (github.event_name != 'pull_request' || github.event.pull_request.head.repo.full_name == github.repository)"
    ' && contains(fromJSON(\'["nestorcolt","agenticore-app[bot]","tcc-main-ci[bot]"]\'), github.actor)'
    ' && contains(fromJSON(\'["nestorcolt","agenticore-app[bot]","tcc-main-ci[bot]"]\'), github.triggering_actor)'
    " || (github.event_name == 'merge_group' || github.event_name == 'push' && github.ref == 'refs/heads/dev')"
    " && github.actor == 'github-merge-queue[bot]' && github.triggering_actor == 'github-merge-queue[bot]')"
)


def pool(label):
    return "${{ " + TRUSTED + " && '" + label + "' || 'ubuntu-latest' }}"


MOVED = {
    ("ci.yml", "lint"): "anton-oss-light",
    ("deploy-assets.yml", "validate"): "anton-oss-light",
}

HOSTED = {
    ("ci.yml", "lint-test"): "ubuntu-latest",
    ("ci.yml", "macos-smoke"): "macos-latest",
    ("docker-build.yml", "build"): "ubuntu-latest",
}


def jobs(name):
    return yaml.safe_load((WORKFLOWS / name).read_text())["jobs"]


@pytest.mark.parametrize(("workflow", "job"), sorted(MOVED))
def test_non_docker_linux_jobs_run_on_the_anton_pool_only_for_trusted_actors(workflow, job):
    assert jobs(workflow)[job]["runs-on"] == pool(MOVED[(workflow, job)])


@pytest.mark.parametrize(("workflow", "job"), sorted(HOSTED))
def test_docker_and_macos_jobs_stay_hosted(workflow, job):
    assert jobs(workflow)[job]["runs-on"] == HOSTED[(workflow, job)]


def test_no_workflow_reaches_an_anton_pool_except_through_the_trusted_expression():
    for path in sorted(WORKFLOWS.glob("*.yml")):
        for name, job in yaml.safe_load(path.read_text())["jobs"].items():
            if "anton" in str(job.get("runs-on", "")):
                assert (path.name, name) in MOVED, f"{path.name}:{name}"


def test_pull_request_target_workflows_stay_on_hosted_runners():
    for path in sorted(WORKFLOWS.glob("*.yml")):
        workflow = yaml.safe_load(path.read_text())
        events = workflow.get("on", workflow.get(True, {}))
        if "pull_request_target" in ([events] if isinstance(events, str) else events):
            assert "anton" not in path.read_text(), path.name


def test_moved_jobs_need_no_docker_daemon():
    for workflow, name in MOVED:
        job = jobs(workflow)[name]
        assert "services" not in job and "container" not in job, f"{workflow}:{name}"
        for step in job["steps"]:
            assert "docker/" not in step.get("uses", ""), f"{workflow}:{name}"
            run = step.get("run", "")
            for word in ("docker run", "docker build", "docker push", "docker compose up"):
                assert word not in run, f"{workflow}:{name}: {word}"
