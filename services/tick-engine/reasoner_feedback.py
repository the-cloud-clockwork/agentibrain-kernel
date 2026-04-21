#!/usr/bin/env python3
"""Reasoner feedback — turn AI tick complaints into actionable to-do arcs.

Reads the last N entries of brain-feed/health.jsonl, parses the AI reasoner's
'reason' field for action keywords, and emits a single "tick-feedback" arc per
day at /vault/clusters/<today>/tick-feedback-<date>.md. The brain-keeper triage
routine can pick this up next cycle and act on it.

This closes the loop: tick AI complains → script captures complaint → arc lands
in vault → next tick reads its own arcs → triage acts → AI sees the fix.

Usage:
    python3 reasoner_feedback.py --brain-feed /vault/brain-feed --vault /vault
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ACTION_KEYWORDS = {
    "merge": ["merge", "merged", "merging", "should be merged", "consolidate", "unify"],
    "graduate": ["graduate", "graduated", "should be graduated", "stale", "drowning"],
    "mitigate": ["mitigation", "no mitigation", "needs mitigation", "unblock"],
    "escalate": ["under-escalated", "under-signaled", "more attention"],
    "fragment": ["fragmented", "fragmentation", "over-fragmented", "noise"],
    "edge": ["orphan", "no edges", "missing edges", "edge consistency", "bidirectionality"],
}

LOOKBACK_TICKS = int(os.getenv("BRAIN_FEEDBACK_LOOKBACK", "6"))


def load_recent_health(path: Path, n: int) -> list[dict]:
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    out = []
    for line in lines[-n:]:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def extract_actions(reasons: list[str]) -> dict[str, list[str]]:
    """For each action category, return list of triggering reason snippets."""
    actions: dict[str, list[str]] = {k: [] for k in ACTION_KEYWORDS}
    for reason in reasons:
        rl = reason.lower()
        for action, kws in ACTION_KEYWORDS.items():
            for kw in kws:
                if kw in rl:
                    snippet = reason[:240].replace("\n", " ").strip()
                    if snippet not in actions[action]:
                        actions[action].append(snippet)
                    break
    return {k: v for k, v in actions.items() if v}


def render_arc(actions: dict[str, list[str]], ticks: list[dict], today: str) -> str:
    avg_score = sum(t.get("score", 0) for t in ticks) / max(1, len(ticks))
    latest = ticks[-1] if ticks else {}
    fm_lines = [
        "---",
        f"cluster_id: {today}-tick-feedback",
        "title: Tick AI Feedback — Auto-generated Action Backlog",
        "region: pineal",
        "status: active",
        "heat: 4",
        "source_sessions: []",
        "project: brain-keeper",
        f"created: {datetime.now(timezone.utc).isoformat()}",
        f"signals: {{tick_count: {len(ticks)}, avg_health: {avg_score:.1f}}}",
        "edges: []",
        "synthesized: true",
        "---",
        "",
        f"# Tick AI Feedback — {today}",
        "",
        "Auto-generated from `brain-feed/health.jsonl` reasoner complaints. ",
        f"Aggregated across the last **{len(ticks)} ticks** "
        f"(avg health = **{avg_score:.1f}/10**, latest = **{latest.get('score', '?')}/10**).",
        "",
        "## Recommended Actions",
        "",
    ]
    if not actions:
        fm_lines.append("_No actionable patterns detected. Health score may be low for non-obvious reasons — read raw reasons below._")
    else:
        for action, snippets in actions.items():
            fm_lines.append(f"### `{action}`")
            fm_lines.append("")
            for s in snippets[:3]:
                fm_lines.append(f"- {s}")
            fm_lines.append("")

    fm_lines.append("## Raw Reasoner Output (latest tick)")
    fm_lines.append("")
    fm_lines.append("> " + str(latest.get("reason", "(no reason)"))[:600].replace("\n", "\n> "))
    fm_lines.append("")
    fm_lines.append("## How to Act")
    fm_lines.append("")
    fm_lines.append("Brain-keeper picks this arc up on next triage cycle. Or invoke manually:")
    fm_lines.append("")
    fm_lines.append("```")
    fm_lines.append(f"# Run brain-keeper triage on the actions in this arc")
    fm_lines.append(f'mcp__tools-aigateway__litellm_tools-invoke_model brain-keeper "triage based on {today}-tick-feedback"')
    fm_lines.append("```")
    fm_lines.append("")
    return "\n".join(fm_lines)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--brain-feed", required=True)
    p.add_argument("--vault", required=True)
    p.add_argument("--lookback", type=int, default=LOOKBACK_TICKS)
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    health_file = Path(args.brain_feed) / "health.jsonl"
    ticks = load_recent_health(health_file, args.lookback)
    if not ticks:
        print("No tick health entries to process", file=sys.stderr)
        return 0

    reasons = [t.get("reason", "") for t in ticks if t.get("reason")]
    actions = extract_actions(reasons)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out_dir = Path(args.vault) / "clusters" / today
    out_dir.mkdir(parents=True, exist_ok=True, mode=0o777)
    out_path = out_dir / f"tick-feedback-{today}.md"

    arc = render_arc(actions, ticks, today)

    if args.dry_run:
        print(arc)
        return 0

    out_path.write_text(arc, encoding="utf-8")
    print(f"✓ wrote {out_path} ({len(actions)} action categories, {len(ticks)} ticks aggregated)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
