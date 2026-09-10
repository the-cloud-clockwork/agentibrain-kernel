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
