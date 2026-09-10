"""Integration tests — build a wheel, install it into a throwaway venv, and
confirm the CLI + migrations + brain profile resolve. Catches the "migrations
not packaged" class of bug that the path-walk fallback silently hid before.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import zipfile
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
        [str(venv / "bin" / "agentibrain"), "--version"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert "agentibrain" in r.stdout.lower()

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


@pytest.mark.skipif(not shutil.which("python3"), reason="python3 required for wheel build")
def test_wheel_ships_brain_profile_without_git(tmp_path):
    """Build from a copy with no .git.

    A git checkout hides a missing MANIFEST.in: setuptools picks tracked files
    up on its own, so the hidden `.claude/` overlay lands in the wheel anyway.
    The sdist-derived build a PyPI consumer gets has no such list.
    """
    source = tmp_path / "src"
    shutil.copytree(
        REPO_ROOT,
        source,
        ignore=shutil.ignore_patterns(".git", "build", "*.egg-info", "__pycache__", ".venv"),
    )

    dist = tmp_path / "dist"
    proc = subprocess.run(
        [sys.executable, "-m", "build", "--wheel", "--outdir", str(dist), str(source)],
        check=False,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        pytest.skip(f"wheel build failed (likely missing `build` package): {proc.stderr[:200]}")

    wheels = list(dist.glob("agentibrain-*.whl"))
    assert wheels, f"no wheel produced under {dist}"

    with zipfile.ZipFile(wheels[0]) as zf:
        names = set(zf.namelist())
    for rel in (
        "agentibrain/profiles/brain/profile.yml",
        "agentibrain/profiles/brain/CLAUDE.md",
        "agentibrain/profiles/brain/.claude/.mcp.json",
    ):
        assert rel in names, rel

    venv = tmp_path / "venv"
    subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
    py = venv / "bin" / "python"
    subprocess.run([str(py), "-m", "pip", "install", "--quiet", str(wheels[0])], check=True)

    r = subprocess.run(
        [str(venv / "bin" / "agentibrain"), "install", "--dry-run"],
        check=True,
        capture_output=True,
        text=True,
    )
    flat = " ".join(r.stdout.split())
    assert "link-profile link" in flat
    assert "site-packages/agentibrain/profiles/brain" in flat
