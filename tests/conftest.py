"""Suite-wide isolation from the operator's real environment.

`install`, `check`, `tick` and `sync` all declare click options carrying
`envvar=` — `KB_ROUTER_TOKEN`, `BRAIN_URL`. Any test driving one of them
through CliRunner therefore reads whatever the developer's shell exports: it
silently overrides the test's own stubs, and a mismatch renders the live value
into a pytest assertion diff. That is a credential disclosure, not a flake.

Scrubbed for every test. A test that wants one of these sets it explicitly.
"""

import pytest

_OPERATOR_ENV = (
    "KB_ROUTER_TOKEN",
    "BRAIN_URL",
    "BRAIN_HTTP_TOKEN",
    "BRAIN_WRITER_OUTBOX",
    "BRAIN_ENABLED",
    "BRAIN_WRITER_ENABLED",
    "OPENAI_API_KEY",
    "LLM_API_KEY",
    "LLM_API_BASE",
    "INFERENCE_URL",
    "INFERENCE_API_KEY",
    "EMBEDDINGS_API_KEY",
    "EMBEDDINGS_API_KEYS",
)


@pytest.fixture(autouse=True)
def _no_operator_env(monkeypatch):
    for name in _OPERATOR_ENV:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def _no_real_deployment(monkeypatch):
    """No test may discover the machine's actual compose stack.

    find_deployment walks up from the cwd — under pytest, this repo — and then
    falls back to the AGENTIBRAIN_REPO pin inside the operator's own
    ~/.agentibrain/.env. Either route reaches a real .env holding a live
    KB_ROUTER_TOKEN, which a failing assertion renders straight into the diff.
    Scrubbing the environment does not cover this; the path is the filesystem.

    A test that wants a deployment stubs it back, which is the opt-in.
    """
    from agentibrain import bootstrap

    monkeypatch.setattr(bootstrap, "find_deployment", lambda *a, **kw: None)


@pytest.fixture(autouse=True)
def _no_real_home(monkeypatch, tmp_path):
    """No test may write into the operator's real ~/.agentibrain.

    Patching Path.home is necessary but not sufficient. config_dir and
    vault_path used to capture Path.home() at import, freezing it into the
    settings class's field defaults where nothing set later could move it —
    which is how this suite overwrote an operator's real config.yaml, leaving
    vault_path on a pytest tmp dir and sending the next scaffold into /tmp.
    Both now resolve per call, config_dir from AGENTIBRAIN_HOME, so setting
    that variable is what actually isolates the write.
    """
    from pathlib import Path

    from agentibrain import config as _config

    real_home = Path.home()
    fake_home = tmp_path / "_home"
    (fake_home / ".agentibrain").mkdir(parents=True)

    monkeypatch.setenv("HOME", str(fake_home))
    # AGENTIBRAIN_HOME is what config_dir() actually reads, and it is resolved
    # per call — patching the module constants alone is cosmetic, and patching
    # the pydantic field default is inert without a model_rebuild.
    monkeypatch.setenv("AGENTIBRAIN_HOME", str(fake_home / ".agentibrain"))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    monkeypatch.setattr(_config, "DEFAULT_CONFIG_DIR", fake_home / ".agentibrain")
    monkeypatch.setattr(_config, "DEFAULT_CONFIG_PATH", fake_home / ".agentibrain" / "config.yaml")

    assert Path.home() != real_home, "Path.home() still returns the real home — refusing to run"
    resolved = _config.config_dir()
    assert real_home not in resolved.parents, (
        f"config_dir() still resolves under the real home ({resolved}) — refusing to run"
    )
    yield
