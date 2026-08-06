"""Pytest fixtures for the embeddings service.

The service's `src/` is a flat module dir (bare `import embed`, `import db`,
`import auth`), so tests put it on sys.path here. Modules read env eagerly at
import time; tests that need a specific model reload `embed` after setting it.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


@pytest.fixture()
def reload_embed(monkeypatch: pytest.MonkeyPatch):
    """Return a helper that sets LLM_EMBED_MODEL / EMBED_DIM then reloads embed
    so target_dim() sees the new values (they are captured at import)."""

    def _reload(model: str | None = None, embed_dim: str | None = None):
        if model is None:
            monkeypatch.delenv("LLM_EMBED_MODEL", raising=False)
        else:
            monkeypatch.setenv("LLM_EMBED_MODEL", model)
        if embed_dim is None:
            monkeypatch.delenv("EMBED_DIM", raising=False)
        else:
            monkeypatch.setenv("EMBED_DIM", embed_dim)
        import embed

        return importlib.reload(embed)

    return _reload
