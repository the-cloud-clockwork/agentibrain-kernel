import importlib.util
import sys
from pathlib import Path

import pytest

AMYGDALA = Path(__file__).resolve().parents[1] / "amygdala.py"


@pytest.fixture
def amygdala(monkeypatch):
    monkeypatch.setitem(sys.modules, "redis", None)
    spec = importlib.util.spec_from_file_location("amygdala_missing_client", AMYGDALA)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "args",
    [
        ["--vault", "{vault}", "--brain-feed", "{vault}/brain-feed"],
        ["--vault", "{vault}", "--brain-feed", "{vault}/brain-feed", "--continuous"],
        ["--replay"],
    ],
    ids=["once", "continuous", "replay"],
)
def test_check_without_redis_client_fails(amygdala, monkeypatch, tmp_path, capsys, args):
    argv = [a.replace("{vault}", str(tmp_path)) for a in args]
    monkeypatch.setattr(sys, "argv", ["amygdala.py", *argv])
    monkeypatch.setattr(amygdala, "run_continuous", lambda *a, **k: pytest.fail("consumer started"))

    assert amygdala.redis is None
    assert amygdala.main() == 1
    out = capsys.readouterr()
    assert out.err == "ERROR: amygdala cannot check signals: redis package not installed\n"
    assert out.out == ""
    assert not (tmp_path / "brain-feed").exists()
