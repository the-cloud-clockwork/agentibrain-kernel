#!/usr/bin/env python3
"""Apply AI tick recommendations to the vault. Closes the loop.

Reads the AI reasoning output (structured markdown), parses the 5 sections,
and applies changes deterministically to vault files:

1. Missing Edges → insert <!-- @edge --> markers into arc files
2. Merge/Split → merge arc files (concat + update frontmatter) or split
3. Signal Escalation → update <!-- @signal severity= --> or remove cleared signals
4. Operator Intent → write to brain-feed/intent.md
5. Brain Health → append score to brain-etl/health.jsonl

Also produces a diff report: what changed this tick vs last tick.

Usage:
    # Parse AI output and apply to vault
    python3 brain_apply.py --vault /vault --brain-feed /vault/brain-feed --ai-output /tmp/ai-tick.md

    # Dry run
    python3 brain_apply.py --vault /vault --brain-feed /vault/brain-feed --ai-output /tmp/ai-tick.md --dry-run
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import markers


# Edge-type priority. Used when collapsing multi-type same-pair collisions.
# Structural hierarchy first; sibling/peer; directional dependencies; the
# generic catch-all last. Anything outside this set (causal, temporal, AI
# hallucinations) sorts after `related` via _rank().
EDGE_TYPE_PRIORITY = ("parent", "child", "sibling", "unblocks", "supersedes", "related")


def _rank(t: str) -> int:
    try:
        return EDGE_TYPE_PRIORITY.index(t)
    except ValueError:
        return len(EDGE_TYPE_PRIORITY)


# ── Parsers for AI tick output sections ───────────────────────────────

def parse_edges(section: str) -> list[dict]:
    """Parse '### 1. Missing Edges' section.

    Backticks around the whole line (`` `arc-a --related--> arc-b` ``) get
    absorbed into the `\\S+` groups by the regex, so strip them after capture.
    Without this, `arc-a` parses as `arc-a\\`` and find_arc_file() silently
    fails — edges are dropped and the AI re-emits them every tick.

    Also drops self-loops at parse time.
    """
    edges = []
    for line in section.splitlines():
        m = re.match(r'`?(\S+)\s+--(\w+)-->\s+(\S+)`?\s*[—–-]\s*(.*)', line)
        if m:
            source = m.group(1).strip("`")
            target = m.group(3).strip("`")
            if source == target:
                continue  # self-loops pollute connectivity metrics
            edges.append({
                "source": source,
                "type": m.group(2),
                "target": target,
                "reason": m.group(4).strip(),
            })
    return edges


def parse_merges(section: str) -> list[dict]:
    """Parse '### 2. Merge/Split Candidates' section."""
    ops = []
    for line in section.splitlines():
        merge_m = re.match(r'`?MERGE:\s*(\S+)\s*\+\s*(\S+)\s*[→→]\s*(.*?)`?$', line)
        split_m = re.match(r'`?SPLIT:\s*(\S+)\s*[→→]\s*(.*?)`?$', line)
        if merge_m:
            ops.append({"op": "merge", "arc_a": merge_m.group(1), "arc_b": merge_m.group(2), "title": merge_m.group(3).strip()})
        elif split_m:
            ops.append({"op": "split", "arc": split_m.group(1), "into": split_m.group(2).strip()})
    return ops


def parse_signals(section: str) -> list[dict]:
    """Parse '### 3. Signal Escalation' section.

    Accepts both single-token sources (`ESCALATE: foo → critical (...)`) and
    backtick-wrapped multi-word sources (`ESCALATE: \`foo bar baz\` → critical (...)`).
    Pre-v2 regex captured only one token via `(\S+)`, which broke on any AI
    output naming multi-word sources and silently no-oped the apply phase.
    """
    changes = []
    for line in section.splitlines():
        line = line.strip()
        # Strip any leading list/bullet marker ('- ', '* ', '1. ', backticks)
        stripped = re.sub(r'^[-*0-9.\s`]+', '', line)
        esc_m = re.match(
            r'(?:ESCALATE):\s*`?([^`→]+?)`?\s*[→]\s*(\w+)\s*\((.*?)\)',
            stripped,
        )
        clr_m = re.match(
            r'(?:CLEAR):\s*`?([^`(]+?)`?\s*\((.*?)\)',
            stripped,
        )
        if esc_m:
            changes.append({
                "op": "escalate",
                "source": esc_m.group(1).strip(),
                "new_severity": esc_m.group(2),
                "reason": esc_m.group(3),
            })
        elif clr_m:
            changes.append({
                "op": "clear",
                "source": clr_m.group(1).strip(),
                "reason": clr_m.group(2),
            })
    return changes


def parse_health(section: str) -> dict:
    """Parse '### 5. Brain Health' section."""
    m = re.search(r'(\d+)/10\s*[—–-]\s*(.*)', section)
    if m:
        return {"score": int(m.group(1)), "reason": m.group(2).strip()}
    return {"score": 0, "reason": "unparseable"}


def parse_intent(section: str) -> str:
    """Parse '### 4. Operator Intent' section — just the text."""
    lines = [l.strip() for l in section.splitlines() if l.strip() and not l.startswith("###")]
    return " ".join(lines)


def parse_ai_output(text: str) -> dict:
    """Parse the full AI tick output into structured sections."""
    sections = {}
    current_key = None
    current_lines = []

    for line in text.splitlines():
        if line.startswith("### 1."):
            if current_key:
                sections[current_key] = "\n".join(current_lines)
            current_key = "edges"
            current_lines = []
        elif line.startswith("### 2."):
            if current_key:
                sections[current_key] = "\n".join(current_lines)
            current_key = "merges"
            current_lines = []
        elif line.startswith("### 3."):
            if current_key:
                sections[current_key] = "\n".join(current_lines)
            current_key = "signals"
            current_lines = []
        elif line.startswith("### 4."):
            if current_key:
                sections[current_key] = "\n".join(current_lines)
            current_key = "intent"
            current_lines = []
        elif line.startswith("### 5."):
            if current_key:
                sections[current_key] = "\n".join(current_lines)
            current_key = "health"
            current_lines = []
        else:
            current_lines.append(line)

    if current_key:
        sections[current_key] = "\n".join(current_lines)

    return {
        "edges": parse_edges(sections.get("edges", "")),
        "merges": parse_merges(sections.get("merges", "")),
        "signals": parse_signals(sections.get("signals", "")),
        "intent": parse_intent(sections.get("intent", "")),
        "health": parse_health(sections.get("health", "")),
    }


# ── Apply functions ───────────────────────────────────────────────────

def find_arc_file(vault_root: Path, arc_id: str) -> Path | None:
    """Find an arc file by cluster_id across all date directories."""
    for date_dir in (vault_root / "clusters").iterdir():
        if not date_dir.is_dir():
            continue
        for md_file in date_dir.glob("*.md"):
            if md_file.name.startswith("_") or ".merged." in md_file.name:
                continue
            try:
                fm, _ = markers.parse_frontmatter(md_file.read_text())
                if fm.get("cluster_id") == arc_id or md_file.stem == arc_id:
                    return md_file
            except Exception:
                continue
    return None


def apply_edges(vault_root: Path, edges: list[dict], dry_run: bool) -> int:
    """Insert @edge markers into source arc files.

    Two-pass:
      1. Group AI-emitted edges by (src_norm, tgt_norm); keep the
         highest-priority type per pair (so a tick that emits both
         `child` and `parent` to the same target lands one canonical
         edge).
      2. For each chosen edge, reject if ANY edge already targets that
         arc from the same source on disk — regardless of type. This is
         the cross-tick guard that prevents multi-type accumulation over
         time (the bug that produced 295 collisions in the live vault).
    """
    applied = 0

    # Pass 1: per-(src,tgt) priority collapse within this tick.
    chosen: dict[tuple[str, str], dict] = {}
    for edge in edges:
        src_norm = edge["source"].strip("`")
        tgt_norm = edge["target"].strip("`")
        if src_norm == tgt_norm:
            continue  # self-loop
        key = (src_norm, tgt_norm)
        if key not in chosen or _rank(edge["type"]) < _rank(chosen[key]["type"]):
            chosen[key] = {**edge, "source": src_norm, "target": tgt_norm}

    # Pass 2: write markers, guarded by any-type-to-same-target check.
    for (src_norm, tgt_norm), edge in chosen.items():
        src_file = find_arc_file(vault_root, src_norm)
        if not src_file:
            print(f"  SKIP edge: source {src_norm} not found", file=sys.stderr)
            continue

        text = src_file.read_text()

        # Cross-tick guard: any existing @edge from this source to this
        # target (any type) blocks the write. One edge per pair, forever.
        any_to_target = re.compile(
            r"<!--\s*@edge\s+type=\w+\s+target=" + re.escape(tgt_norm) + r"\s*-->"
        )
        if any_to_target.search(text):
            continue

        marker = f'<!-- @edge type={edge["type"]} target={tgt_norm} -->'

        # Insert before ## Source sessions (or at end)
        if "## Source sessions" in text:
            text = text.replace("## Source sessions", f"{marker}\n\n## Source sessions")
        elif "## Edges" in text:
            text = text.replace("## Edges", f"## Edges\n\n{marker}")
        else:
            text += f"\n{marker}\n"

        if not dry_run:
            src_file.write_text(text)
        applied += 1
        print(f"  +edge: {src_norm} --{edge['type']}--> {tgt_norm}")

    return applied


def apply_signal_changes(vault_root: Path, changes: list[dict], dry_run: bool) -> int:
    """Update or remove @signal markers based on AI recommendations.

    Primary match path: @signal markers carrying `source=<name>` attribute.
    Fallback path: signal body contains a ~30-char substring of the change
    source name. This lets the apply phase act on legacy @signal markers
    emitted without a structured source= attr (historically common — any
    shell heredoc inject could create such markers).
    """
    applied = 0
    for change in changes:
        source = change["source"]
        source_slug = source[:30]
        # Find all arcs containing signals from this source
        for date_dir in (vault_root / "clusters").iterdir():
            if not date_dir.is_dir():
                continue
            for md_file in date_dir.glob("*.md"):
                if md_file.name.startswith("_"):
                    continue
                text = md_file.read_text()
                has_source_attr = f'source={source}' in text
                # Fuzzy fallback: substring of source in @signal body, not in source= attr
                fuzzy_hit = (
                    not has_source_attr
                    and source_slug in text
                    and '<!-- @signal' in text
                )
                if not has_source_attr and not fuzzy_hit:
                    continue

                if change["op"] == "clear":
                    pattern = re.compile(
                        r'<!-- @signal[^>]*source=' + re.escape(source) + r'[^>]*-->'
                        r'(.*?)'
                        r'<!-- @/signal -->',
                        re.DOTALL,
                    )
                    match = pattern.search(text)
                    if not match and fuzzy_hit:
                        # Fuzzy fallback: match any @signal block whose body
                        # contains the source slug (works for legacy markers
                        # lacking source= attr).
                        pattern = re.compile(
                            r'<!-- @signal[^>]*-->\s*\n'
                            r'([^<]*?' + re.escape(source_slug) + r'[^<]*?)\n'
                            r'<!-- @/signal -->',
                            re.DOTALL,
                        )
                        match = pattern.search(text)
                    if match:
                        content = match.group(1).strip()
                        # Tombstone: if already resolved+cleared, remove entirely
                        if "severity=resolved" in match.group(0) and "(CLEARED:" in content:
                            text = pattern.sub("", text, count=1)
                            print(f"  TOMBSTONE: {change['source']} in {md_file.name}")
                        else:
                            replacement = f'<!-- @signal severity=resolved source={change["source"]} -->\n{content} (CLEARED: {change["reason"]})\n<!-- @/signal -->'
                            text = pattern.sub(replacement, text, count=1)
                            print(f"  CLEAR: {change['source']} in {md_file.name} ({change['reason']})")
                        if not dry_run:
                            md_file.write_text(text)
                        applied += 1

                elif change["op"] == "escalate":
                    # Update severity — primary: source= attr present
                    old_pattern = re.compile(
                        r'(<!-- @signal\s+)severity=\w+(\s+source=' + re.escape(source) + r')'
                    )
                    # Fuzzy fallback: inject severity=X source=<name> into a
                    # bare `<!-- @signal -->` marker whose body matches.
                    if not old_pattern.search(text) and fuzzy_hit:
                        bare_pattern = re.compile(
                            r'(<!-- @signal)(\s*-->\s*\n[^<]*?' + re.escape(source_slug) + r')',
                        )
                        if bare_pattern.search(text):
                            text = bare_pattern.sub(
                                rf'\1 severity={change["new_severity"]} source={source}\2',
                                text,
                                count=1,
                            )
                            if not dry_run:
                                md_file.write_text(text)
                            applied += 1
                            print(f"  ESCALATE (fuzzy): {source} → {change['new_severity']} in {md_file.name}")
                            continue
                    if old_pattern.search(text):
                        text = old_pattern.sub(
                            rf'\1severity={change["new_severity"]}\2',
                            text,
                            count=1,
                        )
                        if not dry_run:
                            md_file.write_text(text)
                        applied += 1
                        print(f"  ESCALATE: {change['source']} → {change['new_severity']} in {md_file.name}")

    return applied


def apply_merges(vault_root: Path, merges: list[dict], dry_run: bool) -> int:
    """Merge arc files."""
    applied = 0
    for merge in merges:
        if merge["op"] != "merge":
            continue  # splits are more complex, skip for now
        file_a = find_arc_file(vault_root, merge["arc_a"])
        file_b = find_arc_file(vault_root, merge["arc_b"])
        if not file_a or not file_b:
            print(f"  SKIP merge: {merge['arc_a']} or {merge['arc_b']} not found", file=sys.stderr)
            continue

        if not dry_run:
            # Append B's content to A, update A's title
            text_a = file_a.read_text()
            text_b = file_b.read_text()
            fm_a, body_a = markers.parse_frontmatter(text_a)
            fm_b, body_b = markers.parse_frontmatter(text_b)

            # Add merge note
            merge_note = f"\n\n## Merged from {merge['arc_b']}\n\n{body_b.strip()}\n"
            merged = text_a.rstrip() + merge_note

            # Update title in frontmatter if provided
            if merge.get("title"):
                merged = re.sub(r'^title:.*$', f'title: {merge["title"]}', merged, count=1, flags=re.MULTILINE)

            file_a.write_text(merged)

            # Move B to a "merged" state (don't delete — rename with .merged suffix)
            merged_path = file_b.with_suffix(".merged.md")
            shutil.move(str(file_b), str(merged_path))

        applied += 1
        print(f"  MERGE: {merge['arc_a']} + {merge['arc_b']} → {merge.get('title', 'merged')}")

    return applied


def write_intent(brain_feed_dir: Path, intent: str, dry_run: bool) -> None:
    """Write operator intent to brain-feed/intent.md."""
    if not intent:
        return
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    content = f"""---
id: operator-intent
title: Operator Intent
priority: 7
ttl: 1800
severity: info
---

## What the operator is likely doing ({date_str})

{intent}
"""
    if not dry_run:
        brain_feed_dir.mkdir(parents=True, exist_ok=True)
        (brain_feed_dir / "intent.md").write_text(content)
    print(f"  INTENT: {intent[:100]}...")


def append_health(health_file: Path, health: dict, tick_stats: dict, dry_run: bool) -> None:
    """Append health score to a JSONL time series."""
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "score": health["score"],
        "reason": health["reason"],
        "arcs": tick_stats.get("arcs_scanned", 0),
        "signals": tick_stats.get("signals_collected", 0),
        "lessons": tick_stats.get("lessons_collected", 0),
    }
    if not dry_run:
        health_file.parent.mkdir(parents=True, exist_ok=True)
        with open(health_file, "a") as f:
            f.write(json.dumps(entry) + "\n")
    print(f"  HEALTH: {health['score']}/10 — {health['reason'][:80]}")


def generate_diff_report(vault_root: Path, actions: dict) -> str:
    """Generate a human-readable diff of what this tick changed."""
    ts = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
    lines = [
        "---",
        "id: last-tick-diff",
        "title: last-tick-diff",
        "priority: 5",
        "ttl: 7200",
        "severity: info",
        "---",
        "",
        f"## Tick Diff — {ts}",
        "",
        f"Edges added: {actions.get('edges_applied', 0)}",
        f"Signals changed: {actions.get('signals_applied', 0)}",
        f"Merges executed: {actions.get('merges_applied', 0)}",
        f"Health score: {actions.get('health', {}).get('score', '?')}/10",
        "",
    ]
    return "\n".join(lines)


# ── Main ──────────────────────────────────────────────────────────────

def apply(vault_root: Path, brain_feed_dir: Path, ai_output: str, dry_run: bool = False) -> dict:
    """Parse AI output and apply all recommendations to the vault."""
    parsed = parse_ai_output(ai_output)

    print(f"Parsed AI output: {len(parsed['edges'])} edges, {len(parsed['merges'])} merges, "
          f"{len(parsed['signals'])} signal changes, health={parsed['health'].get('score', '?')}/10",
          file=sys.stderr)

    actions = {}

    # Apply edges
    if parsed["edges"]:
        print("\n--- Applying edges ---", file=sys.stderr)
        actions["edges_applied"] = apply_edges(vault_root, parsed["edges"], dry_run)

    # Apply signal changes
    if parsed["signals"]:
        print("\n--- Applying signal changes ---", file=sys.stderr)
        actions["signals_applied"] = apply_signal_changes(vault_root, parsed["signals"], dry_run)

    # Apply merges
    if parsed["merges"]:
        print("\n--- Applying merges ---", file=sys.stderr)
        actions["merges_applied"] = apply_merges(vault_root, parsed["merges"], dry_run)

    # Write intent
    if parsed["intent"]:
        print("\n--- Writing intent ---", file=sys.stderr)
        write_intent(brain_feed_dir, parsed["intent"], dry_run)

    # Append health
    print("\n--- Recording health ---", file=sys.stderr)
    health_file = brain_feed_dir.parent / "brain-etl" / "health.jsonl" if (brain_feed_dir.parent / "brain-etl").exists() else brain_feed_dir / "health.jsonl"
    append_health(health_file, parsed["health"], {}, dry_run)
    actions["health"] = parsed["health"]

    # Diff report
    diff = generate_diff_report(vault_root, actions)
    if not dry_run:
        diff_file = brain_feed_dir / "last-tick-diff.md"
        diff_file.write_text(diff)

    actions["dry_run"] = dry_run
    return actions


def main() -> int:
    p = argparse.ArgumentParser(description="Apply AI tick recommendations to the vault")
    p.add_argument("--vault", required=True, help="Vault root path")
    p.add_argument("--brain-feed", required=True, help="Brain feed directory")
    p.add_argument("--ai-output", required=True, help="Path to AI tick output file")
    p.add_argument("--dry-run", action="store_true", help="Print what would happen without writing")
    args = p.parse_args()

    ai_text = Path(args.ai_output).read_text()
    result = apply(Path(args.vault), Path(args.brain_feed), ai_text, args.dry_run)

    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
