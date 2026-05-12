#!/usr/bin/env python3
"""Deterministic clusterer for the brain-clusters skill.

Consumes the JSON bundle from extract.py and groups sessions into cluster
stubs WITHOUT any LLM reasoning. Pure heuristics:

    - Sessions on same project within TIME_WINDOW_MIN of each other → same candidate
    - Compaction boundary → new candidate
    - Branch swap → new candidate (unless same project, short gap)
    - cluster_id = sha1(sorted(session_ids))[:12] + "-" + slug(first_prompt)
    - region: amygdala if error_markers / joy_markers ratio > 2 AND any session
              has error_markers >= ERROR_NUCLEAR_THRESHOLD; pineal if joy_markers
              dominate and error_markers low; otherwise left/right by cwd/branch
              heuristics
    - heat: deterministic formula — recency, tool-call volume, session count
    - ignition: first_user_prompt of earliest session (truncated)

Outputs cluster-stub markdown files to --out-dir. Each stub has empty
Timeline / Lessons / Resolution sections that a separate LLM pass
(kb_brief via inference-gateway) fills in later.

Usage:
    python3 extract.py --since 24h --project my-project | \\
        python3 cluster.py --out-dir /tmp/clusters/2026-04-09

    # Or two-step with a saved bundle:
    python3 extract.py --since 7d > bundle.json
    python3 cluster.py --bundle bundle.json --out-dir vault/clusters/2026-04-09
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

# ── Tunables ──────────────────────────────────────────────────────────
TIME_WINDOW_MIN = 120  # sessions within 2h on same project → same candidate
ERROR_NUCLEAR_THRESHOLD = 10  # error markers above this → amygdala candidate
JOY_PINEAL_THRESHOLD = 5  # joy markers above this with low errors → pineal
HEAT_MAX = 10

SLUG_RE = re.compile(r"[^a-z0-9]+")
CREATIVE_BRANCH_RE = re.compile(
    r"\b(ideas?|creative|design|vision|strategy|publisher|draft|blog|post)\b",
    re.IGNORECASE,
)
TECH_BRANCH_RE = re.compile(
    r"\b(fix|feat|chore|refactor|ci|infra|deploy|hotfix|bug|test)\b",
    re.IGNORECASE,
)


def slugify(s: str, max_len: int = 40) -> str:
    s = SLUG_RE.sub("-", s.lower()).strip("-")
    return s[:max_len] or "untitled"


def parse_ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def session_sort_key(sess: dict) -> tuple:
    return (sess.get("project", ""), parse_ts(sess["start"]))


def group_sessions(sessions: list[dict]) -> list[list[dict]]:
    """Deterministic grouping pass.

    Sort by (project, start_time). Start a new group when:
      - project changes
      - gap from last session > TIME_WINDOW_MIN
      - compaction boundary on either session
    """
    if not sessions:
        return []

    sorted_sessions = sorted(sessions, key=session_sort_key)
    groups: list[list[dict]] = []
    current: list[dict] = [sorted_sessions[0]]

    for sess in sorted_sessions[1:]:
        prev = current[-1]
        same_project = sess.get("project") == prev.get("project")
        prev_end = parse_ts(prev["end"])
        this_start = parse_ts(sess["start"])
        gap_min = (this_start - prev_end).total_seconds() / 60.0
        has_compaction = (sess.get("compactions", 0) > 0) or (
            prev.get("compactions", 0) > 0
        )

        if same_project and gap_min <= TIME_WINDOW_MIN and not has_compaction:
            current.append(sess)
        else:
            groups.append(current)
            current = [sess]

    groups.append(current)
    return groups


def classify_region(group: list[dict]) -> str:
    """Deterministic region assignment from session signals."""
    total_errors = sum(s.get("error_markers", 0) for s in group)
    total_joy = sum(s.get("joy_markers", 0) for s in group)
    max_errors = max((s.get("error_markers", 0) for s in group), default=0)

    # Amygdala: heavy errors and dominant over joy
    if max_errors >= ERROR_NUCLEAR_THRESHOLD and total_errors > 2 * total_joy:
        return "amygdala"

    # Pineal: joy-heavy, low errors
    if total_joy >= JOY_PINEAL_THRESHOLD and total_errors < total_joy:
        return "pineal"

    # Hemisphere by branch / cwd signal
    sample_branch = " ".join(s.get("git_branch", "") for s in group)
    sample_cwd = " ".join(s.get("cwd", "") for s in group)
    haystack = f"{sample_branch} {sample_cwd}"

    creative = bool(CREATIVE_BRANCH_RE.search(haystack))
    technical = bool(TECH_BRANCH_RE.search(haystack))

    if creative and not technical:
        return "right-hemisphere"
    if technical and not creative:
        return "left-hemisphere"
    # Default: left (most ops work is technical)
    return "left-hemisphere"


def compute_heat(group: list[dict], now: datetime) -> int:
    """Pure deterministic heat score 0..HEAT_MAX."""
    heat = 0

    # +3 recency: latest session ended within 24h
    latest_end = max(parse_ts(s["end"]) for s in group)
    age_h = (now - latest_end).total_seconds() / 3600.0
    if age_h <= 24:
        heat += 3
    elif age_h <= 72:
        heat += 1

    # +2 per 1k tool calls across the cluster (capped at +4)
    total_tools = sum(s.get("num_tool_calls", 0) for s in group)
    heat += min(4, (total_tools // 1000) * 2)

    # +1 per extra session (max +3)
    heat += min(3, max(0, len(group) - 1))

    # +1 if any session had joy markers
    if any(s.get("joy_markers", 0) > 0 for s in group):
        heat += 1

    return min(HEAT_MAX, heat)


def compute_cluster_id(group: list[dict], first_prompt: str) -> str:
    """Stable deterministic id from sorted session ids + slug."""
    sids = sorted(s["session_id"] for s in group)
    h = hashlib.sha1("\n".join(sids).encode("utf-8")).hexdigest()[:12]
    date = parse_ts(group[0]["start"]).strftime("%Y-%m-%d")
    slug = slugify(first_prompt)
    return f"{date}-{slug}-{h}"


def find_sibling_arcs(
    group: list[dict], clusters_root: Path | None, current_id: str, lookback_days: int = 7
) -> list[str]:
    """Scan recent date dirs for arcs sharing any session_id with this group.

    Returns list of cluster_ids (without .md) found in sibling arcs. Used to
    auto-link newly created arcs to prior arcs that captured the same session
    UUID — this is how 'same session continuing across days' becomes a graph
    edge instead of a duplicate arc.
    """
    if not clusters_root or not clusters_root.exists():
        return []
    group_sids = {s["session_id"] for s in group}
    siblings: list[str] = []
    today = datetime.now(timezone.utc).date()
    for date_dir in sorted(clusters_root.iterdir(), reverse=True):
        if not date_dir.is_dir() or not date_dir.name[:4].isdigit():
            continue
        try:
            d = datetime.strptime(date_dir.name[:10], "%Y-%m-%d").date()
        except ValueError:
            continue
        if (today - d).days > lookback_days:
            break
        for f in date_dir.glob("*.md"):
            if f.name == "_dashboard.md" or f.stem == current_id:
                continue
            try:
                head = f.read_text(encoding="utf-8", errors="ignore")[:2048]
            except OSError:
                continue
            if "source_sessions:" not in head:
                continue
            for sid in group_sids:
                if sid in head:
                    siblings.append(f.stem)
                    break
    return siblings


def write_stub(cluster: dict, out_dir: Path, clusters_root: Path | None = None) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True, mode=0o777)
    path = out_dir / f"{cluster['cluster_id']}.md"
    siblings = find_sibling_arcs(
        cluster["sessions"], clusters_root, cluster["cluster_id"]
    )

    group = cluster["sessions"]
    total_user = sum(s.get("num_user_turns", 0) for s in group)
    total_asst = sum(s.get("num_assistant_turns", 0) for s in group)
    total_tools = sum(s.get("num_tool_calls", 0) for s in group)
    total_errors = sum(s.get("error_markers", 0) for s in group)
    total_joy = sum(s.get("joy_markers", 0) for s in group)
    duration_h = sum(s.get("duration_minutes", 0) for s in group) / 60.0

    frontmatter = [
        "---",
        f"cluster_id: {cluster['cluster_id']}",
        f"title: {cluster['title']}",
        f"region: {cluster['region']}",
        f"status: {cluster['status']}",
        f"heat: {cluster['heat']}",
        "source_sessions:",
    ]
    for s in group:
        frontmatter.append(f"  - {s['session_id']}")
    frontmatter += [
        f"project: {group[0].get('project', 'unknown')}",
        f"created: {datetime.now(timezone.utc).isoformat()}",
        f"signals: {{errors: {total_errors}, joy: {total_joy}, tools: {total_tools}}}",
        f"edges: {json.dumps([{'sibling': s} for s in siblings]) if siblings else '[]'}",
        "synthesized: false",
        "---",
        "",
        f"# {cluster['title']}",
        "",
        "## Ignition",
        "",
        f"> {cluster['ignition']}",
        "",
        "## Signals",
        "",
        f"- Duration: {duration_h:.1f}h across {len(group)} session(s)",
        f"- Turns: {total_user} user / {total_asst} assistant / {total_tools} tool",
        f"- Error markers: {total_errors} | Joy markers: {total_joy}",
        "",
        "## Timeline",
        "",
        "<!-- synthesized: false — filled by kb_brief LLM pass via inference-gateway -->",
        "",
        "### Completed",
        "",
        "### Errors / learning",
        "",
        "### Resolution",
        "",
        "## Lessons to reproduce",
        "",
        "<!-- synthesized -->",
        "",
        "## Edges",
        "",
        "<!-- parent / child / sibling / related / unblocks / supersedes -->",
        "",
        "## Source sessions",
        "",
    ]
    for s in group:
        frontmatter.append(
            f"- home-bridge://sessions/{s['session_id']} "
            f"({s.get('num_user_turns', 0)} turns, "
            f"{s.get('duration_minutes', 0) / 60.0:.1f}h, "
            f"branch={s.get('git_branch', '?')})"
        )
    frontmatter.append("")

    path.write_text("\n".join(frontmatter), encoding="utf-8")
    return path


def build_cluster(group: list[dict], now: datetime) -> dict:
    first_prompt = (group[0].get("first_user_prompt") or "").strip()
    title_src = first_prompt.split("\n")[0][:60] or "Untitled session"
    cluster_id = compute_cluster_id(group, title_src)

    # Status: active if latest end within 6h, stalled if > 48h, else complete
    latest_end = max(parse_ts(s["end"]) for s in group)
    age_h = (now - latest_end).total_seconds() / 3600.0
    if age_h <= 6:
        status = "active"
    elif age_h > 48:
        status = "stalled"
    else:
        status = "complete"

    return {
        "cluster_id": cluster_id,
        "title": title_src,
        "region": classify_region(group),
        "status": status,
        "heat": compute_heat(group, now),
        "ignition": first_prompt[:280] or "(no prompt captured)",
        "sessions": group,
    }


def write_dashboard(clusters: list[dict], out_dir: Path) -> Path:
    path = out_dir / "_dashboard.md"
    rows = sorted(clusters, key=lambda c: c["heat"], reverse=True)
    date_str = out_dir.name
    lines = [
        f"# Cluster Dashboard — {date_str}",
        "",
        "| Cluster | Region | Status | Heat | Sessions | Ignition |",
        "|---|---|---|---|---|---|",
    ]
    for c in rows:
        ignition = c["ignition"].replace("|", "\\|").replace("\n", " ")[:80]
        lines.append(
            f"| [[{c['cluster_id']}]] | {c['region']} | {c['status']} | "
            f"{c['heat']} | {len(c['sessions'])} | {ignition} |"
        )
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def main() -> int:
    p = argparse.ArgumentParser(
        description="Deterministic clusterer — consumes extract.py JSON bundle"
    )
    p.add_argument("--bundle", help="Path to JSON bundle file (default: stdin)")
    p.add_argument(
        "--out-dir",
        required=True,
        help="Output directory for cluster stubs",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print cluster plan without writing files",
    )
    args = p.parse_args()

    if args.bundle:
        bundle_text = Path(args.bundle).read_text(encoding="utf-8")
    else:
        bundle_text = sys.stdin.read()
    if not bundle_text.strip():
        print("ERROR: empty input bundle", file=sys.stderr)
        return 2
    bundle = json.loads(bundle_text)

    sessions = bundle.get("sessions", [])
    if not sessions:
        print("No sessions in bundle — nothing to cluster", file=sys.stderr)
        return 0

    now = datetime.now(timezone.utc)
    groups = group_sessions(sessions)
    clusters = [build_cluster(g, now) for g in groups]

    out_dir = Path(args.out_dir)

    if args.dry_run:
        for c in clusters:
            print(
                f"{c['cluster_id']} [{c['region']}/{c['status']}/heat={c['heat']}]"
                f" {len(c['sessions'])} sessions — {c['title']}"
            )
        return 0

    # clusters_root is the parent of date dirs (e.g. /vault/clusters/)
    clusters_root = out_dir.parent if out_dir.parent.exists() else None
    written: list[Path] = []
    for c in clusters:
        written.append(write_stub(c, out_dir, clusters_root=clusters_root))
    dash = write_dashboard(clusters, out_dir)

    print(f"✓ {len(written)} cluster stubs → {out_dir}", file=sys.stderr)
    print(f"✓ dashboard → {dash}", file=sys.stderr)
    for c in clusters:
        print(
            f"  {c['cluster_id']} [{c['region']}/{c['status']}/heat={c['heat']}]",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
