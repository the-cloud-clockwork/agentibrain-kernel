#!/usr/bin/env python3
"""Generate the AI tick prompt from brain_keeper.py's deterministic output.

Runs brain_keeper.py first (47ms), then builds a compressed context prompt
that an LLM can reason over in 3-5 turns instead of 80.

The LLM receives:
  - Pre-computed arc table (heat, region, status, title)
  - Extracted signals, lessons, inject blocks (already parsed by markers.py)
  - Edge map (existing edges between arcs)
  - Delta since last tick (what changed)

The LLM returns:
  - New edges to create (arc A relates to arc B because...)
  - Arcs to merge or split
  - Signals to escalate to amygdala
  - Synthesis suggestions (which arcs need Timeline/Lessons filled)
  - Operator intent guess (what's the operator likely working on next)

Usage:
    # Generate the prompt (pipe to clipboard, file, or direct to API)
    python3 brain_tick_prompt.py --vault /vault --brain-feed /vault/brain-feed

    # Full hybrid tick: deterministic + AI dispatch
    python3 brain_tick_prompt.py --vault /vault --brain-feed /vault/brain-feed --dispatch
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import brain_keeper
import markers


def build_prompt(vault_root: Path, brain_feed_dir: Path) -> tuple[str, dict]:
    """Run deterministic tick, then build the AI reasoning prompt.

    Returns (prompt_text, stats_from_deterministic_tick).
    """
    # 1. Run deterministic tick first
    stats = brain_keeper.tick(vault_root, brain_feed_dir)

    # 2. Collect all arc data for the prompt
    clusters_dir = vault_root / "clusters"
    arcs = []
    for date_dir in sorted(clusters_dir.iterdir()):
        if not date_dir.is_dir():
            continue
        for md_file in sorted(date_dir.glob("*.md")):
            if md_file.name.startswith("_") or ".merged." in md_file.name:
                continue
            try:
                doc = markers.extract_all(md_file)
                arcs.append(doc)
            except Exception:
                continue

    # 3. Build the compressed context
    now = datetime.now(timezone.utc)

    # Arc table
    arc_table = "| # | Arc ID | Heat | Region | Status | Title | Sessions | Markers |\n"
    arc_table += "|---|--------|------|--------|--------|-------|----------|--------|\n"
    for i, arc in enumerate(sorted(arcs, key=lambda a: int(a.frontmatter.get("heat", 0)), reverse=True), 1):
        fm = arc.frontmatter
        cid = fm.get("cluster_id", arc.path.stem if arc.path else "?")
        sessions = fm.get("source_sessions", [])
        sess_count = len(sessions) if isinstance(sessions, list) else 1
        marker_count = len(arc.markers)
        arc_table += (
            f"| {i} | {cid} | {fm.get('heat', '?')} | {fm.get('region', '?')} "
            f"| {fm.get('status', '?')} | {fm.get('title', '?')[:50]} "
            f"| {sess_count} | {marker_count} |\n"
        )

    # Edge map
    edge_lines = []
    for arc in arcs:
        cid = arc.frontmatter.get("cluster_id", arc.path.stem if arc.path else "?")
        for edge in arc.edges:
            etype = edge.attr("type", "related")
            target = edge.attr("target", "?")
            edge_lines.append(f"  {cid} --{etype}--> {target}")
    edge_map = "\n".join(edge_lines) if edge_lines else "  (no edges found — this is a gap)"

    # Signals — dedup by (source, content_hash) so a signal promoted to
    # frontal-lobe/conscious/ doesn't appear twice (once from the cluster
    # arc, once from the promoted copy). The duplicate undermines the "do
    # NOT create new signals for issues already covered" instruction below.
    signal_lines = []
    seen_signals: set[tuple[str, str]] = set()
    for arc in arcs:
        for sig in arc.signals:
            sev = sig.attr("severity", "info")
            src = sig.attr("source", "?")
            content = sig.content.splitlines()[0] if sig.content else "(empty)"
            content_hash = hashlib.sha256(
                (sig.content or "").strip().encode("utf-8")
            ).hexdigest()[:16]
            dedup_key = (src, content_hash)
            if dedup_key in seen_signals:
                continue
            seen_signals.add(dedup_key)
            signal_lines.append(f"  [{sev}] ({src}) {content}")
    signals_text = "\n".join(signal_lines) if signal_lines else "  (none)"

    # Lessons (just count + sample)
    all_lessons = []
    for arc in arcs:
        for lesson in arc.lessons:
            all_lessons.append(lesson.content.splitlines()[0] if lesson.content else "")
    lessons_sample = "\n".join(f"  - {l}" for l in all_lessons[:10])
    if len(all_lessons) > 10:
        lessons_sample += f"\n  ... and {len(all_lessons) - 10} more"

    # Unsynthesized arcs
    unsynthesized = [
        arc.frontmatter.get("cluster_id", arc.path.stem if arc.path else "?")
        for arc in arcs
        if arc.frontmatter.get("synthesized") == "false"
    ]

    prompt = f"""You are brain-keeper's AI reasoning layer. The deterministic layer already ran (47ms):
- {stats.get('arcs_scanned', 0)} arcs scanned, {stats.get('heat_changes', 0)} heat changes
- {stats.get('signals_collected', 0)} signals, {stats.get('lessons_collected', 0)} lessons, {stats.get('inject_blocks_collected', 0)} inject blocks
- {stats.get('promotions', 0)} promotions, {stats.get('demotions', 0)} demotions

Your job: REASON about the data below. Do NOT read files — everything is pre-extracted.

## Arc Table (sorted by heat desc)

{arc_table}

## Edge Map

{edge_map}

## Active Signals

{signals_text}

## Lessons (sample of {len(all_lessons)} total)

{lessons_sample}

## Unsynthesized Arcs (need Timeline/Lessons/Resolution)

{json.dumps(unsynthesized) if unsynthesized else "(all synthesized)"}

## Your Tasks (respond in this exact format)

### 1. Missing Edges
List edges that SHOULD exist based on the arc titles/content but DON'T appear in the edge map.
Format: `arc-id-A --type--> arc-id-B` with a one-line reason.

Permitted type values: parent, child, sibling, unblocks, supersedes, related.
Use the strongest type that applies: parent/child for structural hierarchy,
sibling for peer arcs in the same work thread, unblocks/supersedes for
directional dependencies, related as a last resort.

Emit AT MOST ONE edge per (source, target) pair — never the same pair under
two different types in a single response.

If no missing edges, say "None detected."

### 2. Merge/Split Candidates
Are any arcs actually the same work thread that should be merged? Or is one arc too broad and should be split?
Format: `MERGE: arc-A + arc-B → suggested-title` or `SPLIT: arc-A → arc-A-part1 + arc-A-part2`
If none, say "None."

### 3. Signal Escalation
Should any EXISTING signals be escalated (warning → critical, or critical → nuclear)?
Should any EXISTING signals be CLEARED (the issue was resolved)?
Do NOT create new signals for issues already covered by the active signals listed above — they are already broadcasting. Only escalate, clear, or leave unchanged.

Format:
- `ESCALATE: signal-source → new-severity (reason)`
- `CLEAR: signal-source (reason)`

IMPORTANT source-name rules:
- If the source name contains spaces, WRAP IT IN BACKTICKS:
  `ESCALATE: \`ArgoCD Image Updater GHCR auth broken\` → critical (fleet-wide CD halt)`
- If the source name is a single token (kebab-case slug), no backticks needed:
  `ESCALATE: paper2slides-s3 → warning (all jobs fail)`
- The apply phase uses the source name verbatim — do NOT paraphrase between ticks.

If none, say "No changes."

### 4. Operator Intent
Based on the arc heat distribution and active status, what is the operator most likely working on RIGHT NOW? What will they likely do next? One paragraph max.

### 5. Brain Health
Rate the brain's health 1-10. Consider: coverage (are there gaps in what's tracked?), freshness (are hot things actually hot?), connectivity (enough edges?), signal quality (are amygdala signals actionable?).
One line: `Brain health: N/10 — reason`

Respond ONLY with the 5 sections above. No preamble. No explanation of what you're doing. Just the answers.
"""

    return prompt, stats


def main() -> int:
    p = argparse.ArgumentParser(description="Generate AI tick prompt from deterministic brain state")
    p.add_argument("--vault", required=True, help="Vault root path")
    p.add_argument("--brain-feed", required=True, help="Brain feed output directory")
    p.add_argument("--dispatch", action="store_true", help="Dispatch the prompt to brain-keeper agent")
    p.add_argument("--print-only", action="store_true", help="Just print the prompt, don't dispatch")
    args = p.parse_args()

    vault = Path(args.vault)
    feed = Path(args.brain_feed)

    prompt, stats = build_prompt(vault, feed)

    if args.print_only or not args.dispatch:
        print(prompt)
        print("\n--- DETERMINISTIC STATS ---", file=sys.stderr)
        print(json.dumps(stats, indent=2), file=sys.stderr)
        return 0

    # Dispatch to brain-keeper agent
    # This would call the agenticore REST API
    print(json.dumps({
        "prompt_length": len(prompt),
        "stats": stats,
        "action": "dispatch",
        "note": "Would POST to brain-keeper /jobs endpoint with this prompt",
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
