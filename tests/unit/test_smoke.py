"""Phase 1 smoke tests — package imports + version + config schema."""

from __future__ import annotations

import agentibrain
from agentibrain import cli, config


def test_package_version_present() -> None:
    assert isinstance(agentibrain.__version__, str)
    assert agentibrain.__version__


def test_cli_main_is_click_group() -> None:
    assert hasattr(cli, "main")
    assert cli.main.name == "main"


def test_config_defaults() -> None:
    s = config.BrainSettings(_env_file=None)
    assert s.mode == "local"
    assert s.s3_region == "us-east-1"
    assert s.brain_url.startswith("http")


def test_config_s3_requires_bucket() -> None:
    s = config.BrainSettings(mode="s3", _env_file=None)
    try:
        s.require_s3()
    except ValueError as e:
        assert "s3_bucket" in str(e)
    else:
        raise AssertionError("expected ValueError when mode=s3 and bucket missing")
