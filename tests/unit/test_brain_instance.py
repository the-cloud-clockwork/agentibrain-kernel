"""SV2-MUL-01: a brain installation names itself, and a client checks the name before it talks."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import httpx
import pytest
import yaml
from pydantic import ValidationError

from agentibrain import bootstrap, client
from agentibrain.client import BrainBindingError, BrainClient
from agentibrain.config import BrainSettings

REPO = Path(__file__).resolve().parents[2]
FIXTURES = REPO / "tests" / "fixtures" / "brain_instances"
SCHEMA = json.loads((REPO / "schemas" / "brain-instance-v2.json").read_text())


@pytest.fixture(autouse=True)
def _no_ambient_identity(monkeypatch):
    for name in ("BRAIN_ID", "BRAIN_POLICY_REVISION", "BRAIN_BRAIN_URL", "PORT_BRAIN_API"):
        monkeypatch.delenv(name, raising=False)
    client.brain_instance_binding_failures.clear()


def _install(tmp_path: Path, name: str) -> Path:
    root = tmp_path / name
    vault = tmp_path / f"{name}-vault"
    (vault / "arcs").mkdir(parents=True)
    (vault / "arcs" / "kept.md").write_text(f"{name} arc\n")
    root.mkdir()
    text = (FIXTURES / name / "config.yaml").read_text().replace("VAULT", str(vault))
    (root / "config.yaml").write_text(text)
    (root / ".env").write_text("KB_ROUTER_TOKEN=fixture-bearer\n")
    return root


def _load(root: Path) -> BrainSettings:
    raw = yaml.safe_load((root / "config.yaml").read_text()) or {}
    payload = {k: v for k, v in raw.items() if v is not None}
    return BrainSettings(**payload, config_dir=root, _env_file=None)


def _conforms(record: dict) -> None:
    assert set(SCHEMA["required"]) <= set(record)
    assert set(record) <= set(SCHEMA["properties"])
    assert record["schema"] == SCHEMA["properties"]["schema"]["const"]
    assert re.fullmatch(SCHEMA["properties"]["brain_id"]["pattern"], record["brain_id"])
    assert record["policy_revision"] >= SCHEMA["properties"]["policy_revision"]["minimum"]


def _brain(brain_id: str | None, seen: list[httpx.Request]) -> httpx.MockTransport:
    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/instance" and brain_id is not None:
            return httpx.Response(200, json={"brain_id": brain_id, "policy_revision": 1})
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        return httpx.Response(404, json={"detail": "Not Found"})

    return httpx.MockTransport(answer)


def test_a_legacy_installation_maps_to_the_default_brain(tmp_path):
    root = _install(tmp_path, "default")

    record = _load(root).instance_record()

    _conforms(record)
    assert record["brain_id"] == "default"
    assert record["config_root"] == str(root)
    assert record["endpoint"] == "http://localhost:8103"
    assert record["policy_revision"] == 1


@pytest.mark.parametrize(
    ("name", "endpoint", "revision"),
    [("personal", "http://localhost:8103", 1), ("swarm", "https://brain.swarm.example", 3)],
)
def test_named_instances_carry_their_own_identity(tmp_path, name, endpoint, revision):
    for run in ("first", "second"):
        root = _install(tmp_path / run, name)

        record = _load(root).instance_record()

        _conforms(record)
        assert record == {
            "schema": "brain-instance-v2",
            "brain_id": name,
            "config_root": str(root),
            "endpoint": endpoint,
            "policy_revision": revision,
        }


def test_an_empty_brain_id_setting_is_the_default_brain(monkeypatch, tmp_path):
    monkeypatch.setenv("BRAIN_ID", "")

    assert _load(_install(tmp_path, "default")).brain_id == "default"


def test_the_environment_names_the_brain(monkeypatch, tmp_path):
    monkeypatch.setenv("BRAIN_ID", "swarm")

    assert _load(_install(tmp_path, "default")).brain_id == "swarm"


@pytest.mark.parametrize(
    "project", ["github.com/the-cloud-clockwork/agentihooks", "agentihooks.repo", "Personal", " "]
)
def test_a_project_scope_is_not_a_brain_identity(project):
    with pytest.raises(ValidationError):
        BrainSettings(brain_id=project, _env_file=None)


def test_a_client_reaches_the_brain_it_expects(tmp_path):
    for name in ("personal", "swarm"):
        settings = _load(_install(tmp_path / "a", name))
        seen: list[httpx.Request] = []
        brain = BrainClient(
            settings.brain_url,
            "fixture-bearer",
            expected_brain_id=settings.brain_id,
            transport=_brain(name, seen),
        )

        assert brain.health() == {"status": "ok"}
        assert [r.url.path for r in seen] == ["/instance", "/health"]
        assert "authorization" not in seen[0].headers
        assert seen[1].headers["authorization"] == "Bearer fixture-bearer"
    assert sum(client.brain_instance_binding_failures.values()) == 0


@pytest.mark.parametrize(
    ("answered", "label"), [("personal", "personal"), (None, "None"), ("x/" * 300, "invalid")]
)
def test_a_different_or_silent_brain_fails_binding_before_credentials(answered, label):
    seen: list[httpx.Request] = []
    brain = BrainClient(
        "http://brain.local",
        "fixture-bearer",
        expected_brain_id="swarm",
        transport=_brain(answered, seen),
    )

    with pytest.raises(BrainBindingError) as refused:
        brain.health()

    assert [r.url.path for r in seen] == ["/instance"]
    assert all("authorization" not in r.headers for r in seen)
    assert "fixture-bearer" not in str(refused.value)
    assert str(refused.value).endswith(f"answered {label}")
    assert client.brain_instance_binding_failures == {("swarm", label): 1}


def test_a_non_json_answer_fails_binding():
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text="<html>"))
    brain = BrainClient("http://brain.local", "t", transport=transport)

    with pytest.raises(BrainBindingError):
        brain.verify_instance()


def test_a_legacy_installation_migrates_to_default_keeping_vault_and_credentials(tmp_path):
    root = _install(tmp_path, "default")
    vault_before = (tmp_path / "default-vault" / "arcs" / "kept.md").read_bytes()
    env_before = (root / ".env").read_bytes()
    legacy = yaml.safe_load((root / "config.yaml").read_text())

    bootstrap.write_config(_load(root))
    first = (root / "config.yaml").read_bytes()
    bootstrap.write_config(_load(root))

    migrated = yaml.safe_load(first)
    assert (root / "config.yaml").read_bytes() == first
    assert migrated["brain_id"] == "default"
    assert migrated["policy_revision"] == 1
    assert migrated["vault_path"] == legacy["vault_path"]
    assert (root / ".env").read_bytes() == env_before
    assert (tmp_path / "default-vault" / "arcs" / "kept.md").read_bytes() == vault_before
    assert sorted(p.name for p in root.iterdir()) == [".env", "config.yaml"]


def test_an_interrupted_migration_leaves_the_legacy_config_readable(tmp_path, monkeypatch):
    root = _install(tmp_path, "default")
    before = (root / "config.yaml").read_bytes()

    def crash(*_args, **_kwargs):
        raise OSError("power lost")

    monkeypatch.setattr(bootstrap.os, "replace", crash)
    with pytest.raises(OSError):
        bootstrap.write_config(_load(root))
    monkeypatch.undo()

    assert (root / "config.yaml").read_bytes() == before
    assert sorted(p.name for p in root.iterdir()) == [".env", "config.yaml"]
    assert _load(root).brain_id == "default"
    bootstrap.write_config(_load(root))
    assert yaml.safe_load((root / "config.yaml").read_text())["brain_id"] == "default"
    assert (tmp_path / "default-vault" / "arcs" / "kept.md").read_text() == "default arc\n"


def test_a_named_instance_does_not_rewrite_another_instances_files(tmp_path):
    personal = _install(tmp_path, "personal")
    swarm = _install(tmp_path, "swarm")
    swarm_files = {p.name: p.read_bytes() for p in swarm.iterdir()}

    bootstrap.write_config(_load(personal))
    bootstrap.write_env_file(_load(personal), "fixture-bearer")

    assert {p.name: p.read_bytes() for p in swarm.iterdir()} == swarm_files
    assert "BRAIN_ID=personal" in (personal / ".env").read_text().splitlines()
    shutil.rmtree(personal)
    assert swarm.exists()


def test_the_env_file_carries_the_policy_revision_the_brain_serves(tmp_path):
    root = _install(tmp_path, "swarm")

    bootstrap.write_env_file(_load(root), "fixture-bearer")

    lines = (root / ".env").read_text().splitlines()
    assert "BRAIN_ID=swarm" in lines
    assert "BRAIN_POLICY_REVISION=3" in lines


def test_the_default_brain_writes_no_brain_id_to_the_env_file(tmp_path):
    root = _install(tmp_path, "default")

    bootstrap.write_env_file(_load(root), "fixture-bearer")

    assert not any(
        line.startswith("BRAIN_ID=") for line in (root / ".env").read_text().splitlines()
    )
