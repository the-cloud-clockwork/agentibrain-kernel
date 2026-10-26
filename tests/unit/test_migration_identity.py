"""SV2-MUL-04: migrations record the brain that owns the database and refuse another's."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agentibrain import backends, bootstrap
from agentibrain.config import BrainSettings


class FakePsql:
    def __init__(self, held: str):
        self.held = held
        self.calls: list[list[str]] = []

    def __call__(self, cmd, **kwargs):
        self.calls.append(cmd)
        out = f"{self.held}\n" if "-c" in cmd else ""
        return subprocess.CompletedProcess(cmd, 0, out, "")


@pytest.fixture
def psql(monkeypatch):
    monkeypatch.setattr(bootstrap.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(bootstrap, "_wait_for_postgres", lambda dsn: True)

    def install(held: str) -> FakePsql:
        fake = FakePsql(held)
        monkeypatch.setattr(bootstrap.subprocess, "run", fake)
        return fake

    backends.brain_backend_scope_conflicts.clear()
    return install


def _settings(brain_id: str) -> BrainSettings:
    return BrainSettings(
        brain_id=brain_id,
        config_dir=Path.home() / f".agentibrain-{brain_id}",
        postgres_url="postgresql://swarm@db.shared:5432/brains",
        _env_file=None,
    )


def test_every_migration_carries_the_brain_id(psql):
    fake = psql("swarm")

    lines = bootstrap.run_migrations(_settings("swarm"))

    files = [c for c in fake.calls if "-f" in c]
    assert files and all(c[c.index("-v") + 1] == "brain_id=swarm" for c in files)
    assert "004_brain_instance.sql" in {Path(c[c.index("-f") + 1]).name for c in files}
    assert not any(line.startswith("✗") for line in lines)


def test_a_database_held_by_another_brain_is_reported(psql):
    psql("personal")

    lines = bootstrap.run_migrations(_settings("swarm"))

    assert lines[-1] == "✗ database belongs to brain personal, not swarm"
    assert backends.brain_backend_scope_conflicts["schema"] == 1
