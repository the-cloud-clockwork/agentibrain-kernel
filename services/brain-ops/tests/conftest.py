from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import net_wait  # noqa: E402


@pytest.fixture(autouse=True)
def _reachable_network(request, monkeypatch):
    # Tests that fake urlopen never open a socket, so the real probe would only add its backoff.
    if request.module.__name__.endswith("test_net_wait"):
        return
    monkeypatch.setattr(net_wait, "wait_for", lambda *_a, **_k: 1)
