#!/usr/bin/env python3
"""Deterministic session extractor for the brain-clusters skill.

Scans ~/.claude/projects/*/<session>.jsonl files, filters by time window and
project substring, and emits a structured JSON bundle that the AI side of the
skill consumes to produce semantic cluster markdown files.

Usage:
    python3 extract.py --since 24h [--project my-project] [--min-turns 5]

Output: JSON to stdout. Warnings to stderr.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

RELATIVE_RE = re.compile(r"^(\d+)([smhd])$")

# Heuristic markers for emotional gating
JOY_MARKERS = re.compile(
    r"\b(finally|beautiful|perfect|nice|awesome|excellent|eureka|"
    r"this is it|shipped|works|clean|green)\b",
    re.IGNORECASE,
)
ERROR_MARKERS = re.compile(
    r"\b(broken|failed|error|exception|crash|down|urgent|incident|"
    r"regression|rollback|revert|401|403|429|500|502|503|504)\b",
    re.IGNORECASE,
)
COMPACT_MARKERS = re.compile(r"/compact\b|resuming from.*conversation", re.IGNORECASE)


def parse_since(s: str) -> datetime:
    """Accept relative shorthand ('1h','24h','7d','30m','60s') or ISO timestamp."""
    m = RELATIVE_RE.match(s.strip().lower())
    if m:
        n, unit = int(m.group(1)), m.group(2)
        delta = {
            "s": timedelta(seconds=n),
            "m": timedelta(minutes=n),
            "h": timedelta(hours=n),
            "d": timedelta(days=n),
        }[unit]
        return datetime.now(timezone.utc) - delta
    # ISO fallback
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        raise SystemExit(f"ERROR: cannot parse --since '{s}'")


def iter_content_texts(msg) -> list[str]:
    """Extract text blocks from a Claude Code message payload."""
    out: list[str] = []
    if not isinstance(msg, dict):
        return out
    content = msg.get("content")
    if isinstance(content, str):
        out.append(content)
    elif isinstance(content, list):
        for c in content:
            if isinstance(c, dict):
                if c.get("type") == "text" and isinstance(c.get("text"), str):
                    out.append(c["text"])
                elif c.get("type") == "tool_use" and isinstance(c.get("name"), str):
                    out.append(f"<tool_use:{c['name']}>")
    return out


def parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def project_name(cwd: str) -> str:
    """Derive a short project name from a cwd path."""
    if not cwd:
        return "unknown"
    return Path(cwd).name or "unknown"


def process_session(jsonl_path: Path, since: datetime, until: datetime) -> dict | None:
    """Parse one session jsonl file. Return a session summary dict or None to skip."""
    first_ts: datetime | None = None
    last_ts: datetime | None = None
    num_user = 0
    num_assistant = 0
    num_tool = 0
    first_user_text: str | None = None
    last_user_text: str | None = None
    tool_counter: Counter[str] = Counter()
    joy_hits = 0
    error_hits = 0
    compactions = 0
    cwd = ""
    git_branch = ""
    session_id = jsonl_path.stem

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

                if not cwd:
                    cwd = d.get("cwd") or ""
                if not git_branch:
                    git_branch = d.get("gitBranch") or ""

                ts = parse_ts(d.get("timestamp"))
                if ts is None:
                    continue

                if first_ts is None or ts < first_ts:
                    first_ts = ts
                if last_ts is None or ts > last_ts:
                    last_ts = ts

                t = d.get("type")
                msg = d.get("message") or {}
                texts = iter_content_texts(msg)
                joined = " ".join(texts)

                if t == "user":
                    num_user += 1
                    if first_user_text is None and texts:
                        first_user_text = joined
                    if texts:
                        last_user_text = joined
                    if COMPACT_MARKERS.search(joined):
                        compactions += 1
                elif t == "assistant":
                    num_assistant += 1
                    # Count tool uses
                    content = msg.get("content")
                    if isinstance(content, list):
                        for c in content:
                            if isinstance(c, dict) and c.get("type") == "tool_use":
                                name = c.get("name") or "unknown"
                                tool_counter[name] += 1
                                num_tool += 1
                    if joy_hits < 50 and JOY_MARKERS.search(joined):
                        joy_hits += 1
                    if error_hits < 50 and ERROR_MARKERS.search(joined):
                        error_hits += 1
    except OSError as e:
        print(f"WARN: cannot read {jsonl_path}: {e}", file=sys.stderr)
        return None

    if first_ts is None or last_ts is None:
        return None

    # Time window filter: keep session if it overlaps the window at all
    if last_ts < since or first_ts > until:
        return None

    duration_min = (last_ts - first_ts).total_seconds() / 60.0

    return {
        "session_id": session_id,
        "project": project_name(cwd),
        "cwd": cwd,
        "git_branch": git_branch,
        "start": first_ts.isoformat(),
        "end": last_ts.isoformat(),
        "duration_minutes": round(duration_min, 1),
        "num_user_turns": num_user,
        "num_assistant_turns": num_assistant,
        "num_tool_calls": num_tool,
        "first_user_prompt": (first_user_text or "")[:300],
        "last_user_prompt": (last_user_text or "")[:300],
        "top_tools": [
            {"name": n, "count": c} for n, c in tool_counter.most_common(10)
        ],
        "compactions": compactions,
        "joy_markers": joy_hits,
        "error_markers": error_hits,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Extract session summaries from Claude Code jsonl transcripts"
    )
    parser.add_argument(
        "--since",
        default="24h",
        help="Time window start: relative '1h','24h','7d','30m' or ISO timestamp",
    )
    parser.add_argument(
        "--until",
        default=None,
        help="Time window end (default: now)",
    )
    parser.add_argument(
        "--project",
        default=None,
        help="Substring filter on cwd path (e.g. 'my-project')",
    )
    parser.add_argument(
        "--min-turns",
        type=int,
        default=5,
        help="Skip sessions with fewer than N user turns (default: 5)",
    )
    parser.add_argument(
        "--projects-dir",
        default=str(Path.home() / ".claude" / "projects"),
        help="Root of Claude Code projects (default: ~/.claude/projects)",
    )
    args = parser.parse_args()

    since = parse_since(args.since)
    until = parse_since(args.until) if args.until else datetime.now(timezone.utc)

    if since >= until:
        print("ERROR: --since must be earlier than --until", file=sys.stderr)
        return 2

    projects_root = Path(args.projects_dir)
    if not projects_root.exists():
        print(f"ERROR: projects dir not found: {projects_root}", file=sys.stderr)
        return 2

    # Discover jsonl files, optionally filtered by project substring
    candidates: list[Path] = []
    for proj_dir in projects_root.iterdir():
        if not proj_dir.is_dir():
            continue
        if args.project and args.project.lower() not in proj_dir.name.lower():
            continue
        for f in proj_dir.glob("*.jsonl"):
            # Fast mtime prefilter — if file was last modified before the window,
            # it definitely doesn't contain in-window content
            try:
                mtime = datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc)
            except OSError:
                continue
            if mtime < since:
                continue
            candidates.append(f)

    sessions: list[dict] = []
    for path in candidates:
        s = process_session(path, since, until)
        if s is None:
            continue
        if s["num_user_turns"] < args.min_turns:
            continue
        sessions.append(s)

    # Sort by start time ascending
    sessions.sort(key=lambda x: x["start"])

    bundle = {
        "window": {
            "since": since.isoformat(),
            "until": until.isoformat(),
            "hours": round((until - since).total_seconds() / 3600.0, 2),
        },
        "filter": {
            "project": args.project,
            "min_turns": args.min_turns,
        },
        "sessions_scanned": len(candidates),
        "sessions_returned": len(sessions),
        "sessions": sessions,
    }

    json.dump(bundle, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
