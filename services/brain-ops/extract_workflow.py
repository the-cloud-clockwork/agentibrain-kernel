#!/usr/bin/env python3
"""Deterministic workflow template extractor for Brain Block 4 (Replay).

Walks a session's JSONL transcript, emits an ordered list of tool_use blocks
with noise filtering and parameter redaction. Output is replayable via the
/replay skill.

Usage:
    python3 extract_workflow.py --session <session-id> [--max-steps 50]
    python3 extract_workflow.py --session <session-id> --format markdown

Output: JSON to stdout by default; markdown table with --format markdown.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

PROJECTS_DIR = Path.home() / ".claude" / "projects"

CORRECTION_RE = re.compile(
    r"\b(no|stop|wrong|actually|revert|undo|that'?s not|don'?t)\b",
    re.IGNORECASE,
)

SECRET_RE = re.compile(
    r"(sk-[A-Za-z0-9]{20,}|ghp_[A-Za-z0-9]{20,}|xox[baprs]-[A-Za-z0-9-]{10,}"
    r"|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{20,}|Bearer\s+[A-Za-z0-9._-]{20,})",
)

ABS_PATH_RE = re.compile(r"/home/[a-z0-9_-]+|/mnt/[a-z0-9_-]+|/root")
HOST_RE = re.compile(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")

READ_ONLY_TOOLS = frozenset({"Read", "Glob", "Grep", "LS"})


def find_session_jsonl(session_id: str) -> Path | None:
    """Locate the JSONL file for a given session_id across all projects."""
    for p in PROJECTS_DIR.glob(f"*/{session_id}.jsonl"):
        return p
    return None


def redact(value: Any) -> Any:
    """Redact secrets, abs paths, and hostnames from tool-use parameters."""
    if isinstance(value, str):
        v = SECRET_RE.sub("<REDACTED_SECRET>", value)
        v = ABS_PATH_RE.sub("<HOME>", v)
        v = HOST_RE.sub("<IP>", v)
        return v[:500]
    if isinstance(value, dict):
        return {k: redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value[:20]]
    return value


def extract_text(msg: dict) -> str:
    """Extract concatenated text content from a message payload."""
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            c.get("text", "")
            for c in content
            if isinstance(c, dict) and c.get("type") == "text"
        )
    return ""


def extract_workflow(
    session_id: str,
    max_steps: int = 50,
    drop_read_dupes: bool = True,
) -> list[dict]:
    """Walk JSONL, return ordered tool_use steps with noise filtered out.

    Returns: [{"step": N, "tool": str, "params": dict, "turn_idx": int}, ...]
    """
    jsonl_path = find_session_jsonl(session_id)
    if jsonl_path is None:
        raise FileNotFoundError(f"session {session_id} not found under {PROJECTS_DIR}")

    raw_turns: list[dict] = []
    try:
        with open(jsonl_path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                raw_turns.append(d)
    except OSError as e:
        raise RuntimeError(f"cannot read {jsonl_path}: {e}") from e

    steps: list[dict] = []
    suspect_ranges: list[tuple[int, int]] = []

    for idx, d in enumerate(raw_turns):
        if d.get("type") != "user":
            continue
        user_text = extract_text(d.get("message") or {})
        if CORRECTION_RE.search(user_text):
            suspect_ranges.append((max(0, idx - 4), idx))

    def is_suspect(turn_idx: int) -> bool:
        return any(lo <= turn_idx <= hi for lo, hi in suspect_ranges)

    seen_readonly: set[tuple[str, str]] = set()

    for turn_idx, d in enumerate(raw_turns):
        if d.get("type") != "assistant":
            continue
        msg = d.get("message") or {}
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for c in content:
            if not isinstance(c, dict) or c.get("type") != "tool_use":
                continue
            tool_name = c.get("name") or "unknown"
            raw_input = c.get("input") or {}

            if is_suspect(turn_idx):
                continue

            if drop_read_dupes and tool_name in READ_ONLY_TOOLS:
                key_param = (
                    raw_input.get("file_path")
                    or raw_input.get("path")
                    or raw_input.get("pattern")
                    or ""
                )
                fp = (tool_name, str(key_param)[:200])
                if fp in seen_readonly:
                    continue
                seen_readonly.add(fp)

            steps.append(
                {
                    "step": len(steps) + 1,
                    "tool": tool_name,
                    "params": redact(raw_input),
                    "turn_idx": turn_idx,
                }
            )
            if len(steps) >= max_steps:
                return steps

    return steps


def format_markdown(steps: list[dict]) -> str:
    """Render a step list as a markdown table for embedding in arc files."""
    if not steps:
        return "_No deterministic workflow steps extracted._\n"
    lines = ["| Step | Tool | Params |", "|---|---|---|"]
    for s in steps:
        params = json.dumps(s["params"], ensure_ascii=False)[:200].replace("|", "\\|")
        lines.append(f"| {s['step']} | {s['tool']} | `{params}` |")
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Extract ordered tool-use workflow from a Claude Code session JSONL",
    )
    ap.add_argument("--session", required=True, help="session_id (jsonl stem)")
    ap.add_argument("--max-steps", type=int, default=50)
    ap.add_argument(
        "--format",
        choices=("json", "markdown"),
        default="json",
    )
    ap.add_argument(
        "--keep-read-dupes",
        action="store_true",
        help="do not deduplicate Read/Glob/Grep/LS calls on the same target",
    )
    args = ap.parse_args()

    try:
        steps = extract_workflow(
            args.session,
            max_steps=args.max_steps,
            drop_read_dupes=not args.keep_read_dupes,
        )
    except FileNotFoundError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    if args.format == "markdown":
        print(format_markdown(steps))
    else:
        print(json.dumps(steps, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
