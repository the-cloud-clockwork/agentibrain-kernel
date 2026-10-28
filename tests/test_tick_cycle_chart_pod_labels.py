"""The brain-ops chart labels the pods of both tick CronJobs with podLabels,
so a NetworkPolicy can admit them by label."""

import json
import subprocess
from pathlib import Path

import yaml

CHART = Path(__file__).resolve().parents[1] / "helm" / "brain-ops"
LABELS = {"app.kubernetes.io/instance": "swarm-brain-ops", "team": "brain"}


def _pod_templates(*args: str) -> dict:
    rendered = subprocess.run(
        ["helm", "template", "brain", str(CHART), *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return {
        doc["metadata"]["name"]: doc["spec"]["jobTemplate"]["spec"]["template"]
        for doc in yaml.safe_load_all(rendered)
        if doc and doc["kind"] == "CronJob"
    }


def test_pod_labels_render_on_every_cronjob_pod():
    templates = _pod_templates("--set-json", f"podLabels={json.dumps(LABELS)}")
    assert set(templates) == {"brain-brain-ops", "brain-brain-ops-tick-drain"}
    for name, template in templates.items():
        assert template["metadata"]["labels"] == LABELS, name


def test_no_pod_labels_render_no_pod_metadata():
    for name, template in _pod_templates().items():
        assert "metadata" not in template, name
