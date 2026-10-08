"""The portable mcp chart deploys the required transport authentication mode."""

from __future__ import annotations

import subprocess
from pathlib import Path

import yaml

CHART = Path(__file__).resolve().parents[3] / "helm" / "mcp"


def _container() -> dict:
    rendered = subprocess.run(
        ["helm", "template", "agentibrain-mcp", str(CHART)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    for doc in yaml.safe_load_all(rendered):
        if doc and doc["kind"] == "StatefulSet":
            return doc["spec"]["template"]["spec"]["containers"][0]
    raise AssertionError("the chart rendered no StatefulSet")


def test_chart_selects_required_auth_mode():
    env = {item["name"]: item.get("value") for item in _container()["env"]}
    assert env["MCP_AUTH_MODE"] == "required"


def test_key_secret_is_mandatory_and_probes_need_no_key():
    container = _container()
    refs = [ref["secretRef"] for ref in container["envFrom"]]
    assert refs and not any(ref.get("optional") for ref in refs)
    for probe in ("livenessProbe", "readinessProbe"):
        assert container[probe]["httpGet"]["path"] == "/ping"
