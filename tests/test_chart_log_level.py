"""Every service chart overrides the service template's lowercase LOG_LEVEL
default with a level Python logging accepts, because the services pass it
straight to logging.basicConfig."""

import logging
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("chart", ["brain-api", "mcp", "embeddings"])
def test_chart_log_level_is_accepted_by_python_logging(chart):
    values = yaml.safe_load((ROOT / "helm" / chart / "values.yaml").read_text())
    level = values["tpl"]["env"]["variables"]["LOG_LEVEL"]
    assert isinstance(logging.getLevelName(level), int), level
