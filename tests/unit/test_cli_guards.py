"""CLI guards for machines without an `agentibrain init` stack.

up/down/status used to crash with a raw FileNotFoundError when
~/.agentibrain (or its rendered compose.yml) didn't exist — the normal
state on root-compose deployments. Each case runs the CLI in a subprocess
with a scratch HOME because DEFAULT_CONFIG_DIR is resolved from Path.home()
at import time.
"""

import subprocess
import sys

import pytest


def _run_cli(args: list[str], home) -> subprocess.CompletedProcess:
    code = (
        "import sys; sys.argv = ['agentibrain'] + sys.argv[1:]; "
        "from agentibrain.cli import main; main()"
    )
    return subprocess.run(
        [sys.executable, "-c", code, *args],
        capture_output=True,
        text=True,
        env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
        timeout=30,
    )


@pytest.mark.parametrize("cmd", ["up", "down"])
def test_up_down_exit_cleanly_without_init_stack(tmp_path, cmd):
    r = _run_cli([cmd], tmp_path)
    assert r.returncode == 2, r.stderr
    assert "agentibrain init" in r.stdout
    assert "FileNotFoundError" not in r.stderr


def test_status_degrades_gracefully_without_init_stack(tmp_path):
    r = _run_cli(["status"], tmp_path)
    assert r.returncode == 0, r.stderr
    assert "no agentibrain-init stack" in r.stdout
    assert "FileNotFoundError" not in r.stderr
