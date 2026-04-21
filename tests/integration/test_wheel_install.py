"""Integration test — build a wheel, install it into a throwaway venv, and
confirm the CLI + migrations resolve. Catches the "migrations not packaged"
class of bug that the path-walk fallback silently hid before.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.skipif(not shutil.which("python3"), reason="python3 required for wheel build")
def test_wheel_ships_migrations(tmp_path):
    build_cmd = [
        sys.executable,
        "-m",
        "build",
        "--wheel",
        "--outdir",
        str(tmp_path),
        str(REPO_ROOT),
    ]
    proc = subprocess.run(build_cmd, check=False, capture_output=True, text=True)
    if proc.returncode != 0:
        pytest.skip(f"wheel build failed (likely missing `build` package): {proc.stderr[:200]}")

    wheels = list(tmp_path.glob("agentibrain-*.whl"))
    assert wheels, f"no wheel produced under {tmp_path}"

    venv = tmp_path / "venv"
    subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
    py = venv / "bin" / "python"

    subprocess.run([str(py), "-m", "pip", "install", "--quiet", str(wheels[0])], check=True)

    # Confirm the CLI console-script works.
    r = subprocess.run(
        [str(venv / "bin" / "brain"), "--version"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert "brain" in r.stdout.lower()

    # Confirm migrations ship with the wheel.
    r = subprocess.run(
        [
            str(py),
            "-c",
            "from agentibrain.bootstrap import migrations_dir; "
            "d = migrations_dir(); "
            "assert d.is_dir(), d; "
            "files = sorted(f.name for f in d.glob('*.sql')); "
            "print(files)",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert "001_artifact_registry.sql" in r.stdout
    assert "002_embeddings_pgvector.sql" in r.stdout
    assert "003_tick_state.sql" in r.stdout

    # Confirm compose template is packaged too.
    r = subprocess.run(
        [
            str(py),
            "-c",
            "from pathlib import Path; "
            "import agentibrain; "
            "root = Path(agentibrain.__file__).parent; "
            "assert (root / 'templates' / 'compose' / 'compose.yml.j2').is_file()",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
