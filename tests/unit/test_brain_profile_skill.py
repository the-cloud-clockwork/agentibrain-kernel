"""The brain profile ships a skill for markers, ingest and search."""

from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

SKILLS = (
    Path(__file__).resolve().parents[2]
    / "agentibrain"
    / "profiles"
    / "brain"
    / ".claude"
    / "skills"
)
SKILL = SKILLS / "brain-memory"


def _frontmatter(text: str) -> tuple[dict, str]:
    match = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.DOTALL)
    assert match, "SKILL.md must open with YAML frontmatter"
    return yaml.safe_load(match.group(1)), match.group(2)


def test_brain_profile_ships_brain_memory_skill() -> None:
    assert [p.name for p in SKILLS.iterdir() if p.is_dir()] == ["brain-memory"]
    assert (SKILL / "SKILL.md").is_file()


def test_frontmatter_meets_skill_standard() -> None:
    meta, body = _frontmatter((SKILL / "SKILL.md").read_text())
    name, description = meta["name"], meta["description"]
    assert name == SKILL.name
    assert re.fullmatch(r"[a-z0-9-]{1,64}", name)
    assert "claude" not in name and "anthropic" not in name
    assert 0 < len(description) <= 1024
    assert "<" not in description and ">" not in description
    assert len(body.splitlines()) < 500


def test_body_covers_markers_ingest_and_search() -> None:
    _, body = _frontmatter((SKILL / "SKILL.md").read_text())
    assert "`@TYPE`" in body and "`@/TYPE`" in body
    for marker in ("lesson", "decision", "milestone", "signal"):
        assert f"`{marker}`" in body
    for destination in ("left/reference/lessons-", "left/decisions/ADR-", "amygdala/", "daily/"):
        assert destination in body
    assert "/marker" in body
    for tool in (
        "brain_ingest",
        "brain_tick",
        "kb_search",
        "kb_brief",
        "brain_search_arcs",
        "brain_get_arc",
        "vault_list",
        "vault_read",
    ):
        assert f"mcp__agentibrain__{tool}" in body
    bare = re.findall(r"`(brain_\w+|kb_\w+|vault_\w+)`", body)
    assert not bare, f"name MCP tools fully qualified: {bare}"


def test_evals_carry_three_checkable_scenarios() -> None:
    evals = json.loads((SKILL / "evals" / "evals.json").read_text())
    assert len(evals) >= 3
    for case in evals:
        assert case["query"].strip()
        assert case["expected_behavior"] and all(b.strip() for b in case["expected_behavior"])
