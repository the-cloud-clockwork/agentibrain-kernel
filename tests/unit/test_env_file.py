"""``write_env_file`` — the file has to say what it is allowed to configure.

A .env holding only generated secrets tells an operator nothing about which
names the stack reads, and the names are not guessable: the embeddings service
reads LLM_API_KEY, not OPENAI_API_KEY.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from agentibrain import bootstrap
from agentibrain.config import BrainSettings


def _env_text(tmp_path: Path, **kw) -> str:
    settings = BrainSettings(
        mode="local",
        vault_path=tmp_path / "v",
        config_dir=tmp_path / "cfg",
        _env_file=None,
        **kw,
    )
    return bootstrap.write_env_file(settings, "token-for-the-test").read_text()


def _commented(text: str) -> set[str]:
    return {
        line[1:].split("=", 1)[0]
        for line in text.splitlines()
        if line.startswith("#") and "=" in line and not line.startswith("# ")
    }


def test_every_optional_variable_is_named_in_the_file(tmp_path):
    offered = _commented(_env_text(tmp_path))
    assert {
        "LLM_API_KEY",
        "LLM_API_BASE",
        "LLM_EMBED_MODEL",
        "INFERENCE_URL",
        "INFERENCE_API_KEY",
        "TICK_INTERVAL_SECONDS",
    } <= offered
    # Generated, not offered — a commented blank would invite overriding the
    # brain-api → embeddings pair back to nothing.
    assert "EMBEDDINGS_API_KEY" not in offered


def test_ollama_mode_does_not_offer_to_blank_out_its_own_wiring(tmp_path):
    """The compose defaults point these at the bundled Ollama; a commented
    blank here would invite the operator to override them back to nothing."""
    offered = _commented(_env_text(tmp_path, ollama=True))
    assert not {"LLM_API_BASE", "INFERENCE_URL", "LLM_EMBED_MODEL"} & offered


def test_a_supplied_key_is_written_under_the_names_that_are_read(tmp_path):
    text = _env_text(tmp_path, openai_api_key="sk-test-not-a-real-key")
    active = {
        line.split("=", 1)[0] for line in text.splitlines() if line and not line.startswith("#")
    }
    assert {"LLM_API_KEY", "INFERENCE_API_KEY"} <= active
    assert "OPENAI_API_KEY" not in active


def test_rerunning_init_never_costs_the_operator_their_configuration(tmp_path):
    """Re-running init must not clobber a rotated token or a hand-set key.

    The file is the deployment's live credentials — the running stack holds the
    old token, so a silently regenerated one 401s the CLI against its own brain.
    """
    first = _env_text(tmp_path)
    env_path = tmp_path / "cfg" / ".env"

    # Operator rotates the token and sets a provider by hand.
    edited = (
        first.replace(
            "KB_ROUTER_TOKEN=token-for-the-test", "KB_ROUTER_TOKEN=rotated-by-the-operator"
        )
        + "LLM_API_KEY=set-by-hand\nLLM_API_BASE=https://my-proxy.example/v1\n"
    )
    env_path.write_text(edited)

    second = _env_text(tmp_path)  # init run again
    active = dict(
        line.split("=", 1) for line in second.splitlines() if line and not line.startswith("#")
    )
    assert active["KB_ROUTER_TOKEN"] == "rotated-by-the-operator"
    assert active["LLM_API_KEY"] == "set-by-hand"
    assert active["LLM_API_BASE"] == "https://my-proxy.example/v1"
    # Nothing was missing, so the rerun leaves the file exactly as edited.
    assert second == edited


def test_the_internal_embeddings_key_is_stable_across_runs(tmp_path):
    first = _env_text(tmp_path)
    key = dict(
        line.split("=", 1) for line in first.splitlines() if line and not line.startswith("#")
    )["EMBEDDINGS_API_KEY"]
    second = _env_text(tmp_path)
    again = dict(
        line.split("=", 1) for line in second.splitlines() if line and not line.startswith("#")
    )
    assert again["EMBEDDINGS_API_KEY"] == key
    assert again["EMBEDDINGS_API_KEYS"] == key


def test_a_typed_flag_never_overwrites_a_stored_value(tmp_path):
    _env_text(tmp_path)
    (tmp_path / "cfg" / ".env").write_text("LLM_API_KEY=stale\n")
    text = _env_text(tmp_path, openai_api_key="sk-typed-this-run")
    active = dict(
        line.split("=", 1) for line in text.splitlines() if line and not line.startswith("#")
    )
    assert active["LLM_API_KEY"] == "stale"
    assert active["INFERENCE_API_KEY"] == "sk-typed-this-run"


def test_manifest_adds_every_compose_setting_without_activating_defaults(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text("LOG_LEVEL=DEBUG\n# BRAIN_PROMOTE_HEAT=8\n")
    compose = """
services:
  brain:
    environment:
      LOG_LEVEL: ${LOG_LEVEL:-INFO}
      BRAIN_PROMOTE_HEAT: ${BRAIN_PROMOTE_HEAT:-5}
      NEW_SETTING: ${NEW_SETTING:-enabled}
      TOKEN: ${TOKEN:-}
"""

    added = bootstrap.sync_env_manifest(env_path, compose)
    body = env_path.read_text()

    assert {"AGENTIBRAIN_TICK_WAIT_SECONDS", "NEW_SETTING", "TOKEN"} <= set(added)
    assert body.startswith("LOG_LEVEL=DEBUG\n# BRAIN_PROMOTE_HEAT=8\n")
    assert body.count("LOG_LEVEL=") == 1
    assert body.count("BRAIN_PROMOTE_HEAT=") == 1
    assert "# NEW_SETTING=enabled\n" in body
    assert "# TOKEN=\n" in body
    assert bootstrap.sync_env_manifest(env_path, compose) == []


def test_generated_manifest_covers_the_rendered_stack(tmp_path):
    settings = BrainSettings(
        mode="local",
        vault_path=tmp_path / "v",
        config_dir=tmp_path / "cfg",
        _env_file=None,
    )
    env_path = bootstrap.write_env_file(settings, "token-for-the-test")
    known = bootstrap._known_assignments(env_path)
    stack = bootstrap.compose_env_defaults(bootstrap.render_compose(settings))

    assert set(stack) <= known
    assert "AGENTIBRAIN_TICK_WAIT_SECONDS" in known
    assert "BRAIN_PROMOTE_HEAT" in known
    assert "BRAIN_VERIFIER_ENABLED" in known
    assert "KB_RRF_K" in known


def test_rendered_stack_uses_configured_brain_api_port(tmp_path):
    settings = BrainSettings(
        PORT_BRAIN_API=9191,
        vault_path=tmp_path / "v",
        config_dir=tmp_path / "cfg",
        _env_file=None,
    )
    assert "0.0.0.0:9191:8080" in bootstrap.render_compose(settings)


def test_manifest_covers_service_environment_variables():
    root = Path(__file__).parents[2]
    used = set()
    for source in (root / "services").rglob("*.py"):
        tree = ast.parse(source.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            if not isinstance(node.args[0], ast.Constant):
                continue
            name = node.args[0].value
            func = node.func
            if (
                isinstance(name, str)
                and re.fullmatch(r"[A-Z][A-Z0-9_]*", name)
                and isinstance(func, ast.Attribute)
                and func.attr in {"getenv", "get"}
                and (
                    isinstance(func.value, ast.Name)
                    and func.value.id in {"os", "_os"}
                    or isinstance(func.value, ast.Attribute)
                    and func.value.attr == "environ"
                )
            ):
                used.add(name)

    known = set(bootstrap.PROGRAM_ENV_DEFAULTS)
    known.update(
        bootstrap.compose_env_defaults(bootstrap.render_compose(BrainSettings(_env_file=None)))
    )
    assert used - {"HOSTNAME"} <= known
