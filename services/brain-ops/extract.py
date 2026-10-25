#!/usr/bin/env python3
"""Deterministic session extractor for the brain-clusters skill.

Scans ~/.claude/projects/*/<session>.jsonl files and Codex rollout files under
~/.codex/sessions, filters by time window and project substring, and emits a
structured JSON bundle that the AI side of the skill consumes to produce
semantic cluster markdown files.

Usage:
    python3 extract.py --since 24h [--project my-project] [--min-turns 5]

Output: JSON to stdout. Warnings to stderr.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass, field
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
REMOTE_RE = re.compile(
    r"(?:https://|ssh://(?:git@)?|git@)([A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?)([/:])"
    r"([A-Za-z0-9._-]+)/([A-Za-z0-9._-]+)/?"
)
GIT_TIMEOUT = 5
CODEX_TOOL_ITEMS = frozenset({"function_call", "custom_tool_call", "local_shell_call"})
# Codex records its own injected context as user messages.
CODEX_INJECTED_PREFIXES = ("<", "# AGENTS.md")


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
        raise SystemExit(f"ERROR: cannot parse --since '{s}'") from None


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


def canonical_remote(remote: str) -> str | None:
    """The agentihooks client's project id for an origin URL: host/owner/repo."""
    match = REMOTE_RE.fullmatch(remote)
    if not match:
        return None
    host, separator, owner, repo = match.groups()
    if "." not in host or ".." in host or separator != (":" if remote.startswith("git@") else "/"):
        return None
    repo = repo.removesuffix(".git")
    if owner in (".", "..") or repo in ("", ".", ".."):
        return None
    project_id = f"{host.lower()}/{owner}/{repo}"
    return project_id.lower() if host.lower() == "github.com" else project_id


def _git(cwd: str, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", cwd, *args], capture_output=True, text=True, timeout=GIT_TIMEOUT
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def checkout_identity(cwd: str) -> dict:
    if not cwd or not Path(cwd).is_dir():
        return {"project_id": None, "worktree": ""}
    common = _git(cwd, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if not common:
        return {"project_id": None, "worktree": ""}
    top = _git(cwd, "rev-parse", "--show-toplevel")
    return {
        "project_id": canonical_remote(_git(cwd, "remote", "get-url", "origin")),
        "worktree": top if top and top != str(Path(common).parent) else "",
    }


def session_attribution(cwd: str, remote: str = "") -> dict:
    """Project fields of a session: its recorded remote first, else its checkout; never a guess."""
    checkout = checkout_identity(cwd)
    project_id = (canonical_remote(remote) if remote else None) or checkout["project_id"]
    if not project_id:
        return {"project": project_name(cwd), "project_id": None, "worktree": ""}
    return {
        "project": project_id.rsplit("/", 1)[-1],
        "project_id": project_id,
        "worktree": checkout["worktree"],
    }


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
        with open(jsonl_path, encoding="utf-8", errors="replace") as fh:
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
        **session_attribution(cwd),
        "cwd": cwd,
        "harness": "claude",
        "git_branch": git_branch,
        "start": first_ts.isoformat(),
        "end": last_ts.isoformat(),
        "duration_minutes": round(duration_min, 1),
        "num_user_turns": num_user,
        "num_assistant_turns": num_assistant,
        "num_tool_calls": num_tool,
        "first_user_prompt": (first_user_text or "")[:300],
        "last_user_prompt": (last_user_text or "")[:300],
        "top_tools": [{"name": n, "count": c} for n, c in tool_counter.most_common(10)],
        "compactions": compactions,
        "joy_markers": joy_hits,
        "error_markers": error_hits,
    }


@dataclass
class _CodexTally:
    meta: dict = field(default_factory=dict)
    first_ts: datetime | None = None
    last_ts: datetime | None = None
    user: int = 0
    assistant: int = 0
    first_prompt: str = ""
    last_prompt: str = ""
    tools: Counter = field(default_factory=Counter)
    joy: int = 0
    errors: int = 0
    compactions: int = 0

    def stamp(self, ts: datetime) -> None:
        self.first_ts = ts if self.first_ts is None else min(self.first_ts, ts)
        self.last_ts = ts if self.last_ts is None else max(self.last_ts, ts)

    def message(self, payload: dict) -> None:
        content = payload.get("content")
        parts = content if isinstance(content, list) else [{"text": content}]
        joined = " ".join(
            p["text"] for p in parts if isinstance(p, dict) and isinstance(p.get("text"), str)
        )
        if payload.get("role") == "user":
            if not joined or joined.lstrip().startswith(CODEX_INJECTED_PREFIXES):
                return
            self.user += 1
            self.first_prompt = self.first_prompt or joined
            self.last_prompt = joined
            self.compactions += bool(COMPACT_MARKERS.search(joined))
        elif payload.get("role") == "assistant":
            self.assistant += 1
            self.joy += self.joy < 50 and bool(JOY_MARKERS.search(joined))
            self.errors += self.errors < 50 and bool(ERROR_MARKERS.search(joined))

    def record(self, d: dict) -> None:
        payload = d.get("payload") if isinstance(d.get("payload"), dict) else {}
        if d.get("type") == "session_meta" and not self.meta:
            self.meta = payload
        ts = parse_ts(d.get("timestamp"))
        if ts is None or d.get("type") != "response_item":
            return
        self.stamp(ts)
        if payload.get("type") == "message":
            self.message(payload)
        elif payload.get("type") in CODEX_TOOL_ITEMS:
            self.tools[payload.get("name") or payload.get("type")] += 1


def process_codex_session(jsonl_path: Path, since: datetime, until: datetime) -> dict | None:
    """Parse one Codex rollout file into the same summary shape as a Claude session."""
    tally = _CodexTally()
    try:
        with open(jsonl_path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(d, dict):
                    tally.record(d)
    except OSError as e:
        print(f"WARN: cannot read {jsonl_path}: {e}", file=sys.stderr)
        return None
    if tally.first_ts is None or tally.last_ts is None:
        return None
    if tally.last_ts < since or tally.first_ts > until:
        return None
    meta = tally.meta
    git = meta.get("git") if isinstance(meta.get("git"), dict) else {}
    cwd = str(meta.get("cwd") or "")
    return {
        "session_id": str(meta.get("id") or jsonl_path.stem),
        **session_attribution(cwd, str(git.get("repository_url") or "")),
        "cwd": cwd,
        "harness": "codex",
        "git_branch": str(git.get("branch") or ""),
        "start": tally.first_ts.isoformat(),
        "end": tally.last_ts.isoformat(),
        "duration_minutes": round((tally.last_ts - tally.first_ts).total_seconds() / 60.0, 1),
        "num_user_turns": tally.user,
        "num_assistant_turns": tally.assistant,
        "num_tool_calls": sum(tally.tools.values()),
        "first_user_prompt": tally.first_prompt[:300],
        "last_user_prompt": tally.last_prompt[:300],
        "top_tools": [{"name": n, "count": c} for n, c in tally.tools.most_common(10)],
        "compactions": tally.compactions,
        "joy_markers": tally.joy,
        "error_markers": tally.errors,
    }


def codex_candidates(root: Path, since: datetime) -> list[Path]:
    if not root.is_dir():
        return []
    found = []
    for f in sorted(root.rglob("rollout-*.jsonl")):
        try:
            mtime = datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc)
        except OSError:
            continue
        if mtime >= since:
            found.append(f)
    return found


def codex_sessions(
    paths: list[Path], since: datetime, until: datetime, project: str | None
) -> list[dict]:
    """Parsed Codex sessions in the window, filtered by a substring of their working folder."""
    found = (process_codex_session(path, since, until) for path in paths)
    return [s for s in found if s and (not project or project.lower() in s["cwd"].lower())]


def granted_sessions(parsed: list, min_turns: int, projects: str) -> list[dict]:
    """Sessions with enough turns, limited to the EXTRACT_PROJECTS grant when one is set."""
    granted = {p.strip() for p in projects.split(",")} - {""}
    return [
        s
        for s in parsed
        if s and s["num_user_turns"] >= min_turns and (not granted or s["project_id"] in granted)
    ]


def feed_main(args: argparse.Namespace) -> int:
    """Extract from the session change feed, or commit the batch the cluster step accepted."""
    import session_source

    store = session_source.Store(args.state_dir)
    try:
        if args.commit:
            state = store.commit()
            print(f"committed session feed position {state['position']}", file=sys.stderr)
            return 0
        source, grant = session_source.feed_from_env(dict(os.environ))
        bundle = session_source.extract_batch(
            source, grant, store, args.min_turns, limit=args.limit
        )
    except session_source.Refused as refused:
        print(f"ERROR: {refused.error_class}: {refused.detail}", file=sys.stderr)
        return 2
    lag = bundle["source"][session_source.LAG_METRIC]["after_commit"]
    print(f"{session_source.LAG_METRIC}={lag}", file=sys.stderr)
    json.dump(bundle, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")
    return 0


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
    parser.add_argument(
        "--codex-dir",
        default=str(Path.home() / ".codex" / "sessions"),
        help="Root of Codex rollout sessions, skipped when absent (default: ~/.codex/sessions)",
    )
    parser.add_argument(
        "--source",
        choices=("local", "feed"),
        default=os.environ.get("EXTRACT_SOURCE") or "local",
        help="feed reads the session change feed; local is the legacy native home layout",
    )
    parser.add_argument(
        "--state-dir",
        default=os.environ.get("EXTRACT_STATE_DIR", str(Path.home() / ".agentibrain" / "extract")),
        help="Committed and staged session feed cursor state",
    )
    parser.add_argument("--limit", type=int, default=100, help="Session feed page size")
    parser.add_argument(
        "--commit",
        action="store_true",
        help="Commit the staged session feed batch once its output was accepted",
    )
    args = parser.parse_args()
    if args.source == "feed":
        return feed_main(args)

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

    codex = codex_candidates(Path(args.codex_dir), since)
    parsed = [process_session(path, since, until) for path in candidates]
    parsed += codex_sessions(codex, since, until, args.project)
    candidates += codex

    sessions = granted_sessions(parsed, args.min_turns, os.environ.get("EXTRACT_PROJECTS", ""))

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
