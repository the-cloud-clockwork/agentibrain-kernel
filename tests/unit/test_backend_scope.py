"""SV2-MUL-04: brains that look separate cannot share hidden external state.

The fixture is two named brains, personal and swarm, with overlapping arc ids
and identical project names, bound to one shared Postgres server through
distinct principals and schemas and to distinct vaults. Each misbinding case
points one of them at the other's vault, principal, schema or event stream.
"""

from __future__ import annotations

import hashlib
import io
import json
import subprocess
import tarfile
from pathlib import Path

import pytest
from click.testing import CliRunner

from agentibrain import backends, bootstrap, cli
from agentibrain.backends import BackendBinding, BackendScopeError
from agentibrain.config import BrainSettings

SERVER = "postgresql://{user}@db.shared:5432/brains?options=-csearch_path%3D{schema}"
REDIS = "redis://cache.shared:6379/11"
ARC = "arcs/2026-10-26-shared-arc.md"


def _settings(brain_id: str, vault: Path, env_file: str | None = None) -> BrainSettings:
    return BrainSettings(
        brain_id=brain_id,
        config_dir=Path.home() / f".agentibrain-{brain_id}",
        vault_path=vault,
        _env_file=env_file,
    )


def _env(user: str, schema: str, **extra: str) -> dict[str, str]:
    return {"POSTGRES_URL": SERVER.format(user=user, schema=schema), "REDIS_URL": REDIS, **extra}


def _vault(brain_id: str) -> Path:
    vault = Path.home() / f"{brain_id}-vault"
    (vault / "arcs").mkdir(parents=True, exist_ok=True)
    (vault / ARC).write_text(f"---\nproject: github.com/org/app\n---\n{brain_id} knowledge\n")
    return vault


def _pair(vault: Path | None = None, **swarm_env: str) -> tuple[BackendBinding, BackendBinding]:
    personal = BackendBinding.from_settings(
        _settings("personal", _vault("personal")), _env("personal", "brain_personal")
    )
    swarm = BackendBinding.from_settings(
        _settings("swarm", vault or _vault("swarm")),
        {**_env("swarm", "brain_swarm"), **swarm_env},
    )
    return personal, swarm


def _digest(root: Path) -> dict[str, str]:
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


@pytest.fixture(autouse=True)
def _fresh_counter():
    backends.brain_backend_scope_conflicts.clear()


def test_distinct_principals_schemas_and_vaults_share_nothing():
    personal, swarm = _pair()

    assert backends.scope_conflicts(personal, swarm) == []
    assert personal.database.server == swarm.database.server == "db.shared:5432"
    assert (personal.database.principal, personal.database.schema) == (
        "personal",
        "brain_personal",
    )


def test_named_brains_get_their_own_event_streams_and_keys():
    personal, swarm = _pair()

    assert personal.redis_keys & swarm.redis_keys == frozenset()
    assert "brain:personal:events:brain" in personal.redis_keys
    assert "brain:swarm:amygdala:last_event_ts" in swarm.redis_keys
    assert backends.namespaced("default", "events:brain") == "events:brain"


@pytest.mark.parametrize(
    ("swarm_env", "kind"),
    [
        ({"POSTGRES_URL": SERVER.format(user="personal", schema="brain_swarm")}, "principal"),
        ({"POSTGRES_URL": SERVER.format(user="swarm", schema="brain_personal")}, "schema"),
        ({"EVENT_BUS_STREAM": "brain:personal:events:brain"}, "redis"),
    ],
)
def test_a_shared_principal_schema_or_stream_is_a_conflict(swarm_env, kind):
    personal, swarm = _pair(**swarm_env)

    conflicts = backends.scope_conflicts(personal, swarm)

    assert [k for k, _ in conflicts] == [kind]


def test_a_shared_or_nested_vault_is_a_conflict():
    shared = _vault("personal")
    personal, swarm = _pair(vault=shared)
    assert [k for k, _ in backends.scope_conflicts(personal, swarm)] == ["vault"]

    personal, swarm = _pair(vault=shared / "arcs")
    assert [k for k, _ in backends.scope_conflicts(personal, swarm)] == ["vault"]


def test_compose_service_hosts_are_bundled_per_stack():
    env = {
        "POSTGRES_URL": "postgresql://brain@postgres:5432/brain",
        "REDIS_URL": "redis://redis:6379/11",
    }
    personal = BackendBinding.from_settings(_settings("personal", _vault("personal")), env)
    swarm = BackendBinding.from_settings(
        _settings("swarm", _vault("swarm")),
        {**env, "EVENT_BUS_STREAM": "brain:personal:events:brain"},
    )

    assert personal.database is None and personal.redis is None
    assert backends.scope_conflicts(personal, swarm) == []


def test_a_vault_claimed_by_another_brain_is_refused():
    vault = _vault("personal")
    backends.claim_vault(vault, "personal")
    before = _digest(vault)

    with pytest.raises(BackendScopeError) as refused:
        backends.claim_vault(vault, "swarm")

    assert refused.value.kind == "vault"
    assert _digest(vault) == before
    assert json.loads((vault / backends.VAULT_CLAIM).read_text())["brain_id"] == "personal"
    assert backends.brain_backend_scope_conflicts["vault"] == 1


def test_a_vault_inside_another_brains_vault_is_refused():
    vault = _vault("personal")
    backends.claim_vault(vault, "personal")

    with pytest.raises(BackendScopeError):
        backends.claim_vault(vault / "arcs", "swarm")

    assert not (vault / "arcs" / backends.VAULT_CLAIM).exists()


def test_restoring_one_brains_backup_leaves_the_other_unchanged(tmp_path):
    personal, swarm = _vault("personal"), _vault("swarm")
    backends.claim_vault(personal, "personal")
    backends.claim_vault(swarm, "swarm")
    archive = backends.backup_vault(personal, tmp_path / "personal.tgz", "personal")
    (personal / ARC).write_text("personal knowledge, damaged\n")
    swarm_before = _digest(swarm)

    backends.restore_vault(archive, personal, "personal")

    assert (personal / ARC).read_text().endswith("personal knowledge\n")
    assert _digest(swarm) == swarm_before


def test_a_backup_restored_into_another_brain_is_refused(tmp_path):
    personal, swarm = _vault("personal"), _vault("swarm")
    backends.claim_vault(personal, "personal")
    backends.claim_vault(swarm, "swarm")
    archive = backends.backup_vault(personal, tmp_path / "personal.tgz", "personal")
    swarm_before = _digest(swarm)

    with pytest.raises(BackendScopeError) as refused:
        backends.restore_vault(archive, swarm, "swarm")

    assert refused.value.kind == "backup"
    assert _digest(swarm) == swarm_before


def test_a_backup_without_an_instance_claim_is_refused(tmp_path):
    swarm = _vault("swarm")
    backends.claim_vault(swarm, "swarm")
    archive = tmp_path / "bare.tgz"
    data = b"overwrite\n"
    info = tarfile.TarInfo(f"./{ARC}")
    info.size = len(data)
    with tarfile.open(archive, "w:gz") as tar:
        tar.addfile(info, io.BytesIO(data))
    before = _digest(swarm)

    with pytest.raises(BackendScopeError) as refused:
        backends.restore_vault(archive, swarm, "swarm")

    assert refused.value.kind == "backup"
    assert _digest(swarm) == before


def _install(brain_id: str, vault: Path, env: dict[str, str]) -> Path:
    settings = _settings(brain_id, vault)
    bootstrap.write_config(settings)
    lines = [f"BRAIN_ID={brain_id}", *(f"{k}={v}" for k, v in env.items())]
    (settings.config_dir / ".env").write_text("\n".join(lines) + "\n")
    return settings.config_dir


def test_installed_brains_with_overlapping_backends_are_reported():
    _install("personal", _vault("personal"), _env("personal", "brain_personal"))
    root = _install("swarm", _vault("swarm"), _env("personal", "brain_swarm"))

    conflicts = backends.installed_conflicts(
        _settings("swarm", Path.home() / "swarm-vault", str(root / ".env"))
    )

    assert [(other, kind) for other, kind, _ in conflicts] == [("personal", "principal")]


def test_installed_brains_with_distinct_backends_report_nothing():
    _install("personal", _vault("personal"), _env("personal", "brain_personal"))
    root = _install("swarm", _vault("swarm"), _env("swarm", "brain_swarm"))

    assert (
        backends.installed_conflicts(
            _settings("swarm", Path.home() / "swarm-vault", str(root / ".env"))
        )
        == []
    )


def test_up_refuses_a_brain_sharing_another_brains_principal(monkeypatch):
    _install("personal", _vault("personal"), _env("personal", "brain_personal"))
    root = _install("swarm", _vault("swarm"), _env("personal", "brain_swarm"))
    monkeypatch.setattr(bootstrap, "find_deployment", lambda *a, **kw: ("home", root))
    started: list[str] = []
    monkeypatch.setattr(bootstrap, "compose_up", lambda settings: started.append("up"))

    result = CliRunner().invoke(cli.main, ["--instance", "swarm", "up"])

    assert result.exit_code == 1, result.output
    assert started == []
    assert "brain_backend_scope_conflicts kind=principal count=1" in result.output
    assert not (Path.home() / "swarm-vault" / backends.VAULT_CLAIM).exists()


def test_up_fails_when_migrations_find_another_brains_database(monkeypatch):
    root = _install("swarm", _vault("swarm"), _env("swarm", "brain_swarm"))
    monkeypatch.setattr(bootstrap, "find_deployment", lambda *a, **kw: ("home", root))
    monkeypatch.setattr(bootstrap, "instance_conflicts", lambda settings: [])
    monkeypatch.setattr(
        bootstrap, "compose_up", lambda settings: subprocess.CompletedProcess([], 0, "", "")
    )
    monkeypatch.setattr(
        bootstrap,
        "run_migrations",
        lambda settings: [
            "✓ 004_brain_instance.sql",
            "✗ database belongs to brain personal, not swarm",
        ],
    )

    result = CliRunner().invoke(cli.main, ["--instance", "swarm", "up"])

    assert result.exit_code == 1, result.output
    assert "brain_backend_scope_conflicts kind=schema count=1" in result.output
