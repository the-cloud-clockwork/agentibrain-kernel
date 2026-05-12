#!/usr/bin/env python3
"""Brain Markup Protocol — deterministic marker parser.

Parses YAML frontmatter + inline HTML comment markers from markdown files.
Pure stdlib Python. No LLM. No external dependencies.

Marker syntax:
    Inline:  <!-- @type key=value key=value -->
    Block:   <!-- @type key=value --> content <!-- @/type -->
    Edge:    <!-- @edge type=parent target=cluster-id -->

Frontmatter:
    --- (YAML between --- delimiters, parsed as key: value lines) ---

Usage:
    from markers import extract_all, find_markers, parse_frontmatter

    doc = extract_all(Path("arc.md"))
    print(doc.frontmatter["heat"])
    for lesson in doc.lessons:
        print(lesson.content)
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# ── Regex patterns ────────────────────────────────────────────────────

# Inline marker: <!-- @type key=value key="quoted value" -->
INLINE_RE = re.compile(
    r'<!--\s*@(\w+)((?:\s+\w+=[^\s>]+|\s+\w+="[^"]*")*)\s*-->',
)

# Block marker: <!-- @type attrs --> content <!-- @/type -->
BLOCK_RE = re.compile(
    r'<!--\s*@(\w+)((?:\s+\w+=[^\s>]+|\s+\w+="[^"]*")*)\s*-->'
    r'(.*?)'
    r'<!--\s*@/\1\s*-->',
    re.DOTALL,
)

# Attribute parser: key=value or key="quoted value"
ATTR_RE = re.compile(r'(\w+)=(?:"([^"]*)"|(\S+))')

# Frontmatter delimiters
FM_DELIM = "---"

# Severity ordering for comparisons
SEVERITY_ORDER = {"info": 0, "warning": 1, "critical": 2, "nuclear": 3}


# ── Data classes ──────────────────────────────────────────────────────

@dataclass
class Marker:
    """A single marker found in a document."""
    type: str
    attrs: dict = field(default_factory=dict)
    content: str = ""
    line_start: int = 0
    line_end: int = 0
    is_block: bool = False

    def attr(self, key: str, default: str = "") -> str:
        return self.attrs.get(key, default)

    def attr_int(self, key: str, default: int = 0) -> int:
        try:
            return int(self.attrs.get(key, default))
        except (ValueError, TypeError):
            return default


@dataclass
class DocumentMeta:
    """Full extraction result for a document."""
    path: Optional[Path] = None
    frontmatter: dict = field(default_factory=dict)
    body: str = ""
    markers: list = field(default_factory=list)

    @property
    def hot_spots(self) -> list[Marker]:
        return [m for m in self.markers if m.type == "hot"]

    @property
    def signals(self) -> list[Marker]:
        return [m for m in self.markers if m.type == "signal"]

    @property
    def lessons(self) -> list[Marker]:
        return [m for m in self.markers if m.type == "lesson"]

    @property
    def decisions(self) -> list[Marker]:
        return [m for m in self.markers if m.type == "decision"]

    @property
    def edges(self) -> list[Marker]:
        return [m for m in self.markers if m.type == "edge"]

    @property
    def inject_blocks(self) -> list[Marker]:
        return [m for m in self.markers if m.type == "inject"]

    @property
    def milestones(self) -> list[Marker]:
        return [m for m in self.markers if m.type == "milestone"]

    @property
    def todos(self) -> list[Marker]:
        return [m for m in self.markers if m.type == "todo"]


# ── Parsing functions ─────────────────────────────────────────────────

def _parse_attrs(attr_str: str) -> dict:
    """Parse key=value pairs from a marker attribute string."""
    attrs = {}
    for match in ATTR_RE.finditer(attr_str):
        key = match.group(1)
        value = match.group(2) if match.group(2) is not None else match.group(3)
        attrs[key] = value
    return attrs


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Parse YAML frontmatter from markdown text.

    Returns (frontmatter_dict, body_without_frontmatter).
    Frontmatter is parsed as simple key: value lines (no nested YAML).
    """
    if not text.startswith(FM_DELIM):
        return {}, text

    parts = text.split(FM_DELIM, 2)
    if len(parts) < 3:
        return {}, text

    fm_text = parts[1].strip()
    body = parts[2]

    fm = {}
    for line in fm_text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" in line:
            key, _, value = line.partition(":")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            # Handle list values (simple single-level)
            if value.startswith("[") and value.endswith("]"):
                fm[key] = [v.strip().strip('"').strip("'") for v in value[1:-1].split(",")]
            elif value:
                fm[key] = value
            # Multi-line list (YAML style)
            elif not value:
                fm[key] = []
        elif line.startswith("- ") and fm:
            # Append to the last key's list
            last_key = list(fm.keys())[-1]
            if isinstance(fm[last_key], list):
                fm[last_key].append(line[2:].strip())

    return fm, body


def find_markers(text: str, marker_type: str | None = None) -> list[Marker]:
    """Find all inline and block markers in text.

    Args:
        text: The document body (after frontmatter).
        marker_type: If specified, only return markers of this type.

    Returns:
        List of Marker objects, ordered by position in text.
    """
    markers: list[Marker] = []
    lines = text.splitlines()

    # Find block markers first (they span multiple lines)
    for match in BLOCK_RE.finditer(text):
        mtype = match.group(1)
        if marker_type and mtype != marker_type:
            continue
        attrs = _parse_attrs(match.group(2))
        content = match.group(3).strip()
        # Calculate line numbers
        start_pos = match.start()
        end_pos = match.end()
        line_start = text[:start_pos].count("\n")
        line_end = text[:end_pos].count("\n")
        markers.append(Marker(
            type=mtype,
            attrs=attrs,
            content=content,
            line_start=line_start,
            line_end=line_end,
            is_block=True,
        ))

    # Find inline markers (not part of a block)
    block_ranges = [(m.line_start, m.line_end) for m in markers]
    for match in INLINE_RE.finditer(text):
        mtype = match.group(1)
        if marker_type and mtype != marker_type:
            continue
        pos = match.start()
        line_num = text[:pos].count("\n")
        # Skip if this inline marker is the opening tag of a block we already found
        in_block = any(start <= line_num <= end for start, end in block_ranges)
        if in_block:
            continue
        attrs = _parse_attrs(match.group(2))
        markers.append(Marker(
            type=mtype,
            attrs=attrs,
            content="",
            line_start=line_num,
            line_end=line_num,
            is_block=False,
        ))

    markers.sort(key=lambda m: m.line_start)
    return markers


def find_hot_spots(text: str, min_heat: int = 7) -> list[Marker]:
    """Find @hot markers with heat >= min_heat."""
    return [
        m for m in find_markers(text, "hot")
        if m.attr_int("heat", 0) >= min_heat
    ]


def find_signals(text: str, min_severity: str = "warning") -> list[Marker]:
    """Find @signal markers at or above min_severity."""
    threshold = SEVERITY_ORDER.get(min_severity, 1)
    return [
        m for m in find_markers(text, "signal")
        if SEVERITY_ORDER.get(m.attr("severity", "info"), 0) >= threshold
    ]


def find_inject_blocks(text: str) -> list[Marker]:
    """Find @inject markers."""
    return find_markers(text, "inject")


def extract_all(filepath: Path) -> DocumentMeta:
    """Full extraction: frontmatter + all markers.

    Returns DocumentMeta with all parsed data.
    """
    text = filepath.read_text(encoding="utf-8")
    fm, body = parse_frontmatter(text)
    markers = find_markers(body)
    return DocumentMeta(
        path=filepath,
        frontmatter=fm,
        body=body,
        markers=markers,
    )


# ── Self-test ─────────────────────────────────────────────────────────

def _self_test():
    """Quick self-test of the parser."""
    test_doc = """---
cluster_id: test-arc-001
title: Test Arc
heat: 8
region: left-hemisphere
status: active
source_sessions:
  - abc123
  - def456
---

# Test Arc

## Ignition

Some ignition text.

<!-- @hot heat=9 region=left -->
This is a hot paragraph that should be extracted.
<!-- @/hot -->

<!-- @lesson -->
Always write tests before shipping.
<!-- @/lesson -->

<!-- @signal severity=critical source=auth-broker -->
Token rotation failing intermittently.
<!-- @/signal -->

<!-- @edge type=parent target=parent-arc-001 -->
<!-- @edge type=sibling target=sibling-arc-002 -->

<!-- @inject target=claude.md -->
Test arc is the current priority.
<!-- @/inject -->

<!-- @decision date=2026-04-10 -->
Use deterministic parsers over LLM agents for structured data extraction.
<!-- @/decision -->

<!-- @todo priority=1 -->
Implement the full brain-keeper maintenance tick.
<!-- @/todo -->
"""
    fm, body = parse_frontmatter(test_doc)
    assert fm["cluster_id"] == "test-arc-001", f"Expected test-arc-001, got {fm['cluster_id']}"
    assert fm["heat"] == "8", f"Expected '8', got {fm['heat']}"
    assert fm["region"] == "left-hemisphere"
    assert isinstance(fm["source_sessions"], list)
    assert len(fm["source_sessions"]) == 2

    markers = find_markers(body)
    types = [m.type for m in markers]
    assert "hot" in types, f"Missing @hot marker. Found: {types}"
    assert "lesson" in types, f"Missing @lesson marker. Found: {types}"
    assert "signal" in types, f"Missing @signal marker. Found: {types}"
    assert "edge" in types, f"Missing @edge marker. Found: {types}"
    assert "inject" in types, f"Missing @inject marker. Found: {types}"
    assert "decision" in types, f"Missing @decision marker. Found: {types}"
    assert "todo" in types, f"Missing @todo marker. Found: {types}"

    # Check block content extraction
    hot = [m for m in markers if m.type == "hot"][0]
    assert "hot paragraph" in hot.content, f"Hot content wrong: {hot.content}"
    assert hot.attr_int("heat") == 9

    lesson = [m for m in markers if m.type == "lesson"][0]
    assert "Always write tests" in lesson.content

    signal = [m for m in markers if m.type == "signal"][0]
    assert signal.attr("severity") == "critical"
    assert signal.attr("source") == "auth-broker"

    edges = [m for m in markers if m.type == "edge"]
    assert len(edges) == 2
    assert edges[0].attr("type") == "parent"
    assert edges[0].attr("target") == "parent-arc-001"

    inject = [m for m in markers if m.type == "inject"][0]
    assert "current priority" in inject.content

    # Test DocumentMeta
    import tempfile
    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
        f.write(test_doc)
        f.flush()
        doc = extract_all(Path(f.name))
        assert doc.frontmatter["title"] == "Test Arc"
        assert len(doc.hot_spots) == 1
        assert len(doc.signals) == 1
        assert len(doc.lessons) == 1
        assert len(doc.edges) == 2
        assert len(doc.inject_blocks) == 1
        assert len(doc.decisions) == 1
        assert len(doc.todos) == 1

    # Test convenience functions
    hots = find_hot_spots(body, min_heat=8)
    assert len(hots) == 1

    sigs = find_signals(body, min_severity="critical")
    assert len(sigs) == 1

    sigs_warn = find_signals(body, min_severity="warning")
    assert len(sigs_warn) == 1  # critical >= warning

    injects = find_inject_blocks(body)
    assert len(injects) == 1

    print("All tests passed ✓")
    print(f"  Frontmatter keys: {list(fm.keys())}")
    print(f"  Markers found: {len(markers)} ({', '.join(set(types))})")
    print(f"  Hot spots: {len(doc.hot_spots)}, Signals: {len(doc.signals)}, Lessons: {len(doc.lessons)}")
    print(f"  Edges: {len(doc.edges)}, Inject blocks: {len(doc.inject_blocks)}")


if __name__ == "__main__":
    _self_test()
