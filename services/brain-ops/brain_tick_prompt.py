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
from brain_apply import _rank

# Prompt-size guards. The AI reasoning model has a finite context window
# (claude-max-sonnet: 200K tokens ≈ ~800K chars). An unbounded edge map or arc
# table overflows it — the model then returns an unparseable stub and the tick
# scores 0/10 → nuclear. These caps plus the final char budget keep the prompt
# inside the window regardless of vault size or merge/edge corruption.
MAX_EDGE_LINES = 500        # rendered edge lines after (source, target) dedup
MAX_ARC_ROWS = 250          # arc-table rows, highest heat first
MAX_PROMPT_CHARS = 400_000  # ~100K tokens — hard ceiling enforced before POST
# Synthesis (Task 6). Bounded per tick: an unsynthesized backlog drains over
# successive ticks rather than blowing one prompt. Summaries persist to arc
# frontmatter, so each arc is paid for exactly once.
MAX_SYNTH_ARCS = 25         # arcs offered for summarization per tick
SYNTH_IGNITION_CHARS = 240  # opening-prompt excerpt per arc
SYNTH_MARKER_CHARS = 200    # per marker excerpt


def _squash(text: str, limit: int) -> str:
    """Collapse to a single clean line for prompt embedding."""
    flat = " ".join((text or "").split())
    return flat[:limit] + ("…" if len(flat) > limit else "")


def _extract_section(arc, heading: str) -> str:
    """Pull one `## <heading>` section body out of an arc, stripped of markup."""
    body = getattr(arc, "body", "") or ""
    out: list[str] = []
    capturing = False
    for line in body.splitlines():
        s = line.strip()
        if s.startswith("## "):
            if capturing:
                break
            capturing = s[3:].strip().lower() == heading.lower()
            continue
        if capturing and s and not s.startswith("<!--"):
            out.append(s.lstrip("> ").strip())
    return " ".join(out)


def build_prompt(vault_root: Path, brain_feed_dir: Path) -> tuple[str, dict]:
    """Run deterministic tick, then build the AI reasoning prompt.

    Returns (prompt_text, stats_from_deterministic_tick).
    """
    # 1. Run deterministic tick first
    stats = brain_keeper.tick(vault_root, brain_feed_dir)

    # 2. Collect all arc data for the prompt
    # Walk the full region tree so the prompt sees the same arc set the
    # deterministic keeper does. Prior version scanned only clusters/ and
    # additionally filtered out every .merged.md file — net result was a
    # single arc reaching the AI even though brain_keeper.tick() scans
    # 144+. That undercount drove the health-score floor and the "only 1
    # arc visible / coverage cannot be assessed" complaint that was looping
    # in every tick reason. Mirrors brain_keeper._scan_and_collect
    # (brain_keeper.py:372-395) and reuses brain_keeper.REGION_DIRS as the
    # single source of truth for which region dirs are authoritative.
    clusters_dir = vault_root / "clusters"
    arcs: list[markers.DocumentMeta] = []
    seen_ids: set[str] = set()

    def _scan(directory: Path, recurse: bool = False) -> None:
        if not directory.is_dir():
            return
        files = sorted(directory.glob("**/*.md" if recurse else "*.md"))
        # Dir-bucketed merged_stems — a `.merged.md` in dir X only
        # suppresses the raw counterpart in the SAME dir X. Prior flat
        # set would silently drop an unrelated `foo.md` in a sibling
        # subdir if some other `foo.merged.md` existed elsewhere under
        # the recursive root.
        merged_stems_by_dir: dict[Path, set[str]] = {}
        for f in files:
            if f.name.endswith(".merged.md"):
                merged_stems_by_dir.setdefault(f.parent, set()).add(
                    f.name.replace(".merged.md", "")
                )
        for md_file in files:
            if md_file.name.startswith("_"):
                continue
            stem = md_file.name[:-3]
            # Prefer .merged.md over the raw counterpart in the same dir.
            if (not md_file.name.endswith(".merged.md")
                and stem in merged_stems_by_dir.get(md_file.parent, set())):
                continue
            arc_id = md_file.stem.replace(".merged", "")
            if arc_id in seen_ids:
                continue
            seen_ids.add(arc_id)
            try:
                arcs.append(markers.extract_all(md_file))
            except Exception as e:
                # Surface parse failures — parity with brain_keeper, otherwise
                # a corrupt frontmatter at 270 files-per-scan disappears silently.
                print(f"WARN: failed to parse {md_file}: {e}", file=sys.stderr)
                continue

    # Region dirs first — promoted/graduated arcs are authoritative.
    for region in brain_keeper.REGION_DIRS:
        _scan(vault_root / region, recurse=True)

    # Clusters last — raw date-bucketed tail; dedup against seen_ids
    # silently drops any arc already covered by a region.
    if clusters_dir.is_dir():
        for date_dir in sorted(clusters_dir.iterdir()):
            if date_dir.is_dir():
                _scan(date_dir)

    # 3. Build the compressed context
    now = datetime.now(timezone.utc)

    # Arc table — capped at MAX_ARC_ROWS by heat. Rows scale with vault size;
    # the cap keeps this section bounded (defensive; see prompt-size guards).
    sorted_arcs = sorted(
        arcs, key=lambda a: int(a.frontmatter.get("heat", 0) or 0), reverse=True
    )
    arc_table = "| # | Arc ID | Heat | Region | Status | Title | Sessions | Markers |\n"
    arc_table += "|---|--------|------|--------|--------|-------|----------|--------|\n"
    for i, arc in enumerate(sorted_arcs[:MAX_ARC_ROWS], 1):
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
    if len(sorted_arcs) > MAX_ARC_ROWS:
        arc_table += (
            f"| … | *(+{len(sorted_arcs) - MAX_ARC_ROWS} more arcs, trimmed by heat)* "
            f"| | | | | | |\n"
        )

    # Edge map — dedup per (source, target) keeping the strongest edge type,
    # then hard-cap. This is the only prompt section that scales with the
    # (historically runaway) on-disk edge count; without the dedup + cap a
    # corrupt vault overflows the model context and the tick scores 0/nuclear.
    # Dedup mirrors apply_edges pass-1 (lower _rank == stronger type).
    chosen_edges: dict[tuple[str, str], str] = {}
    edge_order: list[tuple[str, str]] = []
    for arc in arcs:
        cid = arc.frontmatter.get("cluster_id", arc.path.stem if arc.path else "?")
        for edge in arc.edges:
            etype = edge.attr("type", "related")
            target = edge.attr("target", "?")
            key = (cid, target)
            if key not in chosen_edges:
                chosen_edges[key] = etype
                edge_order.append(key)
            elif _rank(etype) < _rank(chosen_edges[key]):
                chosen_edges[key] = etype
    edge_lines = [
        f"  {cid} --{chosen_edges[(cid, target)]}--> {target}"
        for (cid, target) in edge_order
    ]
    total_edges = len(edge_lines)
    if total_edges > MAX_EDGE_LINES:
        edge_lines = edge_lines[:MAX_EDGE_LINES]
        edge_lines.append(f"  ... and {total_edges - MAX_EDGE_LINES} more edges (capped)")
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

    # Lessons — dedup by content_hash so the 10-item sample contains 10
    # distinct entries. Without this, a lesson repeating in many arc files
    # (legitimate per-arc authorship — each writer session captures its own
    # "NFS dirs need chmod 777" lesson when it hits the same issue) dominates
    # the sample and the AI flags it as "lesson dedup broken". The vault
    # data is fine; only the prompt rendering needs dedup. Same pattern as
    # seen_signals above and seen in brain_keeper.write_inject_feed.
    all_lessons = []
    seen_lessons: set[str] = set()
    for arc in arcs:
        for lesson in arc.lessons:
            first = (lesson.content.splitlines()[0] if lesson.content else "").strip()
            if not first:
                continue  # empty lesson body — nothing useful to show the AI
            # Key on first_line, not full content. The AI sees only the
            # first line in the sample, so two lessons sharing a first
            # line are visually identical to the AI even if their bodies
            # differ in metadata footers. Dedup on what's visible.
            key = first.lower()
            if key in seen_lessons:
                continue
            seen_lessons.add(key)
            all_lessons.append(first)
    lessons_sample = "\n".join(f"  - {l}" for l in all_lessons[:10])
    if len(all_lessons) > 10:
        lessons_sample += f"\n  ... and {len(all_lessons) - 10} more"

    # Unsynthesized arcs — the material for Task 6.
    #
    # This list used to be bare cluster_ids, which made the section useless:
    # the model was shown WHICH arcs needed summarizing but never WHAT they
    # contained, and no task ever asked it to summarize them. Carry enough
    # substance per arc that a one-sentence summary is actually derivable.
    def _needs_summary(a) -> bool:
        fm = a.frontmatter
        if str(fm.get("summary", "")).strip():
            return False  # already synthesized, never re-spend on it
        if fm.get("synthesized") == "false":
            return True
        # Arcs written outside cluster.py never carry a `synthesized` field at
        # all — notably the `-writer` marker arcs, which hold the richest
        # content in the vault (real @lesson/@decision/@milestone bodies) and
        # were nonetheless stuck forever on the mechanical title
        # "Session markers — <sid>". Anything with a cluster_id is a work arc
        # and deserves a summary; standing region docs (no cluster_id) keep
        # their hand-written titles and are left alone.
        return bool(str(fm.get("cluster_id", "")).strip())

    unsynth_arcs = [a for a in arcs if _needs_summary(a)]
    unsynth_arcs.sort(key=lambda a: int(a.frontmatter.get("heat", 0) or 0), reverse=True)

    unsynth_blocks: list[str] = []
    for arc in unsynth_arcs[:MAX_SYNTH_ARCS]:
        fm = arc.frontmatter
        cid = fm.get("cluster_id", arc.path.stem if arc.path else "?")
        project = str(fm.get("project", "") or "")
        if "/" in project:
            project = project.rstrip("/").rsplit("/", 1)[-1]
        parts = [
            f"- id: {cid}",
            f"  project: {project or '?'} | created: {fm.get('created', '?')} "
            f"| heat: {fm.get('heat', '?')} | status: {fm.get('status', '?')}",
        ]
        ignition = _extract_section(arc, "Ignition")
        if ignition:
            parts.append(f"  opened_with: {_squash(ignition, SYNTH_IGNITION_CHARS)}")
        # Markers are the highest-signal content an arc carries — a decision or
        # milestone states the outcome directly, which is exactly what a summary
        # should say.
        for label, items in (
            ("decision", arc.decisions),
            ("milestone", arc.milestones),
            ("lesson", arc.lessons),
        ):
            for mk in items[:2]:
                parts.append(f"  {label}: {_squash(mk.content, SYNTH_MARKER_CHARS)}")
        unsynth_blocks.append("\n".join(parts))

    # Previous intent — tick-to-tick continuity. Without this the model
    # re-derives the operator's situation from scratch every 2h and produces a
    # fresh narrative each time instead of tracking a thread.
    previous_intent = "(none — first tick or intent.md absent)"
    try:
        intent_path = brain_feed_dir / "intent.md"
        if intent_path.exists():
            prev = intent_path.read_text(encoding="utf-8")
            if "---" in prev:
                prev = prev.split("---", 2)[-1]
            prev = " ".join(prev.split())
            if prev:
                previous_intent = _squash(prev, 700)
    except OSError:
        pass

    unsynthesized_text = "\n".join(unsynth_blocks) if unsynth_blocks else "(all synthesized)"
    if len(unsynth_arcs) > MAX_SYNTH_ARCS:
        unsynthesized_text += (
            f"\n\n(+{len(unsynth_arcs) - MAX_SYNTH_ARCS} more unsynthesized arcs, "
            "trimmed by heat — they will be offered on subsequent ticks)"
        )

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

## Unsynthesized Arcs (need a summary — see Task 6)

{unsynthesized_text}

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
  `ESCALATE:` followed by the source in backticks, e.g. image-updater registry auth broken -> critical (fleet-wide CD halt)
- If the source name is a single token (kebab-case slug), no backticks needed:
  `ESCALATE: paper2slides-s3 → warning (all jobs fail)`
- The apply phase uses the source name verbatim — do NOT paraphrase between ticks.

If none, say "No changes."

### 4. Operator Intent
What is the operator working on RIGHT NOW, and what will they likely do next?
One paragraph max.

Ground every claim in the data above and cite the arc id or signal you inferred
it from. Prefer summaries, decisions and milestones over arc titles — titles are
scraped from the operator's opening message and are often meaningless ("hey",
tool boilerplate); never build a narrative on a title alone. Name the actual
projects and systems involved rather than describing activity in the abstract.
If the data does not support a confident read, say what is unclear instead of
inventing connective tissue. Where the previous intent below still holds, say
so and describe what changed rather than re-deriving from scratch.

Previous intent (last tick):
{previous_intent}

### 5. Brain Health
Rate the brain's health 1-10. Consider: coverage (are there gaps in what's tracked?), freshness (are hot things actually hot?), connectivity (enough edges?), signal quality (are amygdala signals actionable?).
One line: `Brain health: N/10 — reason`

### 6. Arc Summaries
For EACH arc under "Unsynthesized Arcs" above, write one sentence saying what
that work actually was — what was being built, fixed or decided, and its
outcome if known. This sentence is injected into every future agent session as
the arc's identity, so it must stand alone without the reader seeing the arc.

Format, one per line, id first:
`SUMMARY: <arc-id> | <one sentence>`

Rules:
- Lead with the concrete subject (the system, service or file), not "the operator".
- State outcomes plainly: shipped, fixed, root-caused, abandoned, still open.
- No hedging, no meta ("this arc covers…", "a session about…").
- 30 words max. Plain prose, no markdown, no pipe characters in the sentence.
- If an arc genuinely has too little content to summarize, emit
  `SUMMARY: <arc-id> | SKIP` and it will be offered again next tick.

If there are no unsynthesized arcs, say "None."

Respond ONLY with the 6 sections above. No preamble. No explanation of what you're doing. Just the answers.
"""

    # Final hard ceiling. The per-section caps above keep a healthy vault well
    # under budget; this guarantees the prompt fits the context window on ANY
    # vault state (e.g. before a cleanup has run). Drop the largest variable
    # sections first — edge map, then arc table — before a blunt truncation.
    if len(prompt) > MAX_PROMPT_CHARS:
        prompt = prompt.replace(
            edge_map, "  [edge map omitted — exceeded context budget; run vault cleanup]"
        )
    if len(prompt) > MAX_PROMPT_CHARS:
        prompt = prompt.replace(
            arc_table, "  [arc table omitted — exceeded context budget]\n"
        )
    if len(prompt) > MAX_PROMPT_CHARS:
        prompt = prompt[:MAX_PROMPT_CHARS] + "\n\n[prompt hard-truncated to fit context]\n"

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
