from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from brain_apply import parse_health
from brain_tick import BRAIN_LLM_MAX_TOKENS
from brain_tick_prompt import MAX_PROMPT_CHARS


def test_parse_health_accepts_structured_json():
    assert parse_health('HEALTH_JSON: {"score": 8, "reason": "fresh"}') == {"score": 8, "reason": "fresh"}


def test_parse_health_accepts_prompt_legacy_format():
    assert parse_health("Brain health: 8/10 — fresh") == {"score": 8, "reason": "fresh"}


def test_parse_health_rejects_invalid_score():
    assert parse_health('HEALTH_JSON: {"score": 0, "reason": "bad"}') == {"score": 0, "reason": "unparseable"}


def test_local_inference_defaults_fit_a_small_context():
    assert MAX_PROMPT_CHARS == 12000
    assert BRAIN_LLM_MAX_TOKENS == 512
