#!/usr/bin/env python3
"""Deterministic brain-keeper — vault maintenance in <5 seconds.

Replaces the Sonnet agent approach ($2.16, 9.5 min, FAILED) with
pure Python: parse frontmatter, compute heat, promote/demote files,
generate brain-feed outputs.

Usage:
    python3 brain_keeper.py --vault /vault --brain-feed /vault/brain-feed
    python3 brain_keeper.py --vault /vault --brain-feed /tmp/test --dry-run
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Import sibling module
sys.path.insert(0, str(Path(__file__).parent))
import markers
import brain_verifier


# ── Heat computation ──────────────────────────────────────────────────

HEAT_MAX = 10

# Promote/demote/graduate thresholds. Empirically the heat formula tops out
# at ~5-6 for healthy active arcs; the previous hardcoded promote=7 was
# mathematically unreachable, leaving conscious/ permanently empty.
BRAIN_PROMOTE_HEAT = int(os.getenv("BRAIN_PROMOTE_HEAT", "5"))
BRAIN_DEMOTE_HEAT = int(os.getenv("BRAIN_DEMOTE_HEAT", "3"))
BRAIN_GRADUATE_HEAT = int(os.getenv("BRAIN_GRADUATE_HEAT", "1"))
BRAIN_GRADUATE_AGE_DAYS = int(os.getenv("BRAIN_GRADUATE_AGE_DAYS", "14"))

# Decay: after BRAIN_DECAY_START_DAYS, heat drops -1 per BRAIN_DECAY_INTERVAL_DAYS.
# Prevents arcs from staying hot forever when no new activity references them.
# Defaults softened — previous 2/2 burned arcs in 4 days, faster than ticks
# could promote them. 7/4 keeps an active arc hot for ~2 weeks.
BRAIN_DECAY_START_DAYS = int(os.getenv("BRAIN_DECAY_START_DAYS", "7"))
BRAIN_DECAY_INTERVAL_DAYS = max(1, int(os.getenv("BRAIN_DECAY_INTERVAL_DAYS", "4")))

# Stale signal sweep: signals whose parent arc is older than this and are not
# nuclear/critical get filtered out of signals.md. Prevents broadcast pollution
# from old stress-test debris and orphan signals.
BRAIN_STALE_SIGNAL_DAYS = int(os.getenv("BRAIN_STALE_SIGNAL_DAYS", "3"))
BRAIN_STALE_SIGNAL_KEEP_SEVERITIES = {"nuclear", "critical"}


def compute_heat(
    doc: markers.DocumentMeta,
    now: datetime,
    replay_boost: int = 0,
) -> int:
    """Recompute heat from frontmatter + signals. Pure arithmetic.

    replay_boost: +1 per recent (<14d) arc that references this arc via a
    `replayed_from` edge. Rewards reproducible arcs that get re-used.
    """
    fm = doc.frontmatter
    heat = replay_boost

    # Recency from created date
    created_str = fm.get("created", "")
    if created_str:
        try:
            created = datetime.fromisoformat(created_str.replace("Z", "+00:00"))
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            age_h = (now - created).total_seconds() / 3600.0
            if age_h <= 24:
                heat += 3
            elif age_h <= 72:
                heat += 1
        except (ValueError, TypeError):
            pass

    # Tool volume from signals field. If signals is missing (common on
    # merged arcs and arcs predating cluster.py's signals writer), fall back
    # to source_sessions count alone — better than treating signal-less arcs
    # as zero-volume work.
    signals_str = fm.get("signals", "")
    has_signals = isinstance(signals_str, str) and "tools:" in signals_str
    if has_signals:
        try:
            tools_val = int(signals_str.split("tools:")[1].split(",")[0].split("}")[0].strip())
            heat += min(4, (tools_val // 1000) * 2)
        except (ValueError, IndexError):
            pass

    # Session count — every active session worth +1 (was: -1, capped at 3).
    # Without rich signals data the per-session contribution was the only
    # signal we had to differentiate single-session noise from real work,
    # and it under-weighted single-session arcs that are fresh and active.
    sessions = fm.get("source_sessions", [])
    if isinstance(sessions, list):
        heat += min(3, len(sessions))
    elif isinstance(sessions, str):
        heat += 1

    # Joy markers from signals
    if has_signals and "joy:" in signals_str:
        try:
            joy_val = int(signals_str.split("joy:")[1].split(",")[0].split("}")[0].strip())
            if joy_val > 0:
                heat += 1
        except (ValueError, IndexError):
            pass

    # Status bonus
    status = fm.get("status", "")
    if status == "active":
        heat += 2

    # Decay penalty: after BRAIN_DECAY_START_DAYS of age, lose 1 heat per
    # BRAIN_DECAY_INTERVAL_DAYS. Floors at 0. Only applied when the arc has
    # real age metadata. Replay boost already counteracts this for re-used arcs.
    if created_str:
        try:
            created = datetime.fromisoformat(created_str.replace("Z", "+00:00"))
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            age_days = (now - created).days
            if age_days > BRAIN_DECAY_START_DAYS:
                decay = (age_days - BRAIN_DECAY_START_DAYS) // BRAIN_DECAY_INTERVAL_DAYS
                heat -= decay
        except (ValueError, TypeError):
            pass

    return max(0, min(HEAT_MAX, heat))


# ── File operations ───────────────────────────────────────────────────

def write_hot_arcs_md(path: Path, arcs: list[markers.DocumentMeta]) -> None:
    """Generate brain_adapter-compatible hot-arcs.md."""
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    lines = [
        "---",
        f"id: hot-arcs-{date_str}",
        "title: Active Hot Arcs",
        "priority: 10",
        "ttl: 3600",
        "severity: info",
        "---",
        "",
        f"## Hot Arcs — {date_str}",
        "",
        "| Arc | Heat | Region | Status | Title |",
        "|---|---|---|---|---|",
    ]
    for arc in arcs:
        fm = arc.frontmatter
        cid = fm.get("cluster_id", arc.path.stem if arc.path else "?")
        title = fm.get("title", cid)[:80].replace("|", "-")
        heat = fm.get("heat", "?")
        region = fm.get("region", "?")
        status = fm.get("status", "?")
        lines.append(f"| [[{cid}]] | {heat} | {region} | {status} | {title} |")
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def write_signals_feed(
    path: Path,
    signals_list: list[markers.Marker],
    now: datetime | None = None,
) -> dict:
    """Generate signals.md from collected @signal markers.

    Returns stats dict: {written, tombstoned_stale, tombstoned_cleared, tombstoned_mitigated}.
    Each signal may have attr `_parent_arc_created` set by the collector;
    if the parent arc is older than BRAIN_STALE_SIGNAL_DAYS and severity is
    not in {nuclear, critical}, the signal is tombstoned (filtered out).
    Signals with attr `_mitigated=true` are tombstoned regardless of severity
    (a mitigation arc declares `mitigates: <source>` and is resolved/graduated).
    """
    now = now or datetime.now(timezone.utc)
    stats = {"written": 0, "tombstoned_stale": 0, "tombstoned_cleared": 0, "tombstoned_mitigated": 0}

    if not signals_list:
        path.write_text("---\nid: signals\ntitle: Active Signals\npriority: 8\nttl: 3600\nseverity: warning\n---\n\nNo active signals.\n")
        return stats

    date_str = now.strftime("%Y-%m-%d")
    lines = [
        "---",
        "id: signals",
        "title: Active Signals",
        "priority: 8",
        "ttl: 3600",
        "severity: warning",
        "---",
        "",
        f"## Signals — {date_str}",
        "",
    ]
    cutoff = now - timedelta(days=BRAIN_STALE_SIGNAL_DAYS)
    for sig in signals_list:
        sev = sig.attr("severity", "info")
        src = sig.attr("source", "unknown")
        content_line = sig.content.splitlines()[0] if sig.content else "(empty)"

        # Tombstone: skip resolved signals that have been cleared
        if sev == "resolved" and "(CLEARED:" in content_line:
            stats["tombstoned_cleared"] += 1
            continue

        # Mitigation tombstone: any signal whose source has been mitigated by
        # a resolved/graduated arc (`mitigates: <source>`). Closes nuclear/critical
        # signals — the only way they exit the broadcast besides operator action.
        if sig.attr("_mitigated", "") == "true":
            stats["tombstoned_mitigated"] += 1
            continue

        # Stale sweep: filter signals whose parent arc is too old, unless
        # the severity is protected (nuclear/critical always broadcast).
        if sev not in BRAIN_STALE_SIGNAL_KEEP_SEVERITIES:
            parent_created = sig.attr("_parent_arc_created", "")
            if parent_created:
                try:
                    pc = datetime.fromisoformat(parent_created.replace("Z", "+00:00"))
                    if pc.tzinfo is None:
                        pc = pc.replace(tzinfo=timezone.utc)
                    if pc < cutoff:
                        stats["tombstoned_stale"] += 1
                        continue
                except (ValueError, TypeError):
                    pass

        lines.append(f"- **[{sev}]** ({src}) {content_line}")
        stats["written"] += 1

    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")
    return stats


def write_inject_feed(path: Path, injects: list[markers.Marker]) -> None:
    """Generate inject.md from collected @inject markers. Deduplicates by content hash."""
    if not injects:
        path.write_text("---\nid: inject\ntitle: Brain Inject\npriority: 9\nttl: 3600\nseverity: info\n---\n\nNo inject blocks.\n")
        return
    lines = [
        "---",
        "id: inject",
        "title: Brain Inject",
        "priority: 9",
        "ttl: 3600",
        "severity: info",
        "---",
        "",
    ]
    seen: set[str] = set()
    for inj in injects:
        content_hash = hashlib.sha256(inj.content.strip().encode("utf-8")).hexdigest()[:16]
        if content_hash in seen:
            continue
        seen.add(content_hash)
        target = inj.attr("target", "all")
        lines.append(f"**[{target}]** {inj.content.strip()}")
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def update_dashboard(date_dir: Path, arcs: list[markers.DocumentMeta]) -> None:
    """Update _dashboard.md in a date directory."""
    date_str = date_dir.name
    hot = sorted(arcs, key=lambda a: int(a.frontmatter.get("heat", 0)), reverse=True)
    lines = [
        f"# Cluster Dashboard — {date_str}",
        "",
        "| Arc | Heat | Region | Status | Title |",
        "|---|---|---|---|---|",
    ]
    for arc in hot:
        fm = arc.frontmatter
        cid = fm.get("cluster_id", arc.path.stem if arc.path else "?")
        lines.append(
            f"| [[{cid}]] | {fm.get('heat', '?')} | {fm.get('region', '?')} "
            f"| {fm.get('status', '?')} | {fm.get('title', cid)} |"
        )
    lines.append("")
    (date_dir / "_dashboard.md").write_text("\n".join(lines), encoding="utf-8")


# ── Main tick ─────────────────────────────────────────────────────────

REGION_DIRS = ("bridge", "left", "right", "frontal-lobe", "pineal", "amygdala")

TAG_REGION_MAP = {
    "architecture": "left", "infrastructure": "left", "deployment": "left",
    "code": "left", "bug": "left", "fix": "left", "ci": "left",
    "database": "left", "api": "left", "security": "left",
    "research": "left/research", "incident": "left/incidents",
    "decision": "left/decisions", "reference": "left/reference",
    "idea": "right/ideas", "strategy": "right/strategy",
    "creative": "right/creative", "vision": "right",
    "risk": "right/risk", "life": "right/life",
    "brain-system": "bridge", "cross-cutting": "bridge",
}
INBOX_DEFAULT_REGION = "left"


def drain_inbox(vault_root: Path, dry_run: bool = False) -> dict:
    """Move notes from raw/inbox/ to appropriate region dirs based on tags."""
    inbox = vault_root / "raw" / "inbox"
    if not inbox.is_dir():
        return {"drained": 0, "skipped": 0}

    stats = {"drained": 0, "skipped": 0, "errors": 0}
    for md in sorted(inbox.glob("*.md")):
        try:
            doc = markers.extract_all(md)
        except Exception as e:
            print(f"WARN: inbox parse failed {md.name}: {e}", file=sys.stderr)
            stats["errors"] += 1
            continue

        tags = doc.frontmatter.get("tags", [])
        if isinstance(tags, str):
            tags = [t.strip() for t in tags.split(",")]

        region = INBOX_DEFAULT_REGION
        for tag in tags:
            tag_lower = tag.lower().strip()
            if tag_lower in TAG_REGION_MAP:
                region = TAG_REGION_MAP[tag_lower]
                break

        target_dir = vault_root / region
        if not dry_run:
            target_dir.mkdir(parents=True, exist_ok=True)
            dest = target_dir / md.name
            if dest.exists():
                dest = target_dir / f"{md.stem}-{datetime.now(timezone.utc).strftime('%H%M%S')}{md.suffix}"
            shutil.move(str(md), str(dest))
        stats["drained"] += 1
        print(f"INBOX: {md.name} → {region}/")
    return stats


def tick(vault_root: Path, brain_feed_dir: Path, dry_run: bool = False,
         quick_refresh: bool = False) -> dict:
    """One maintenance tick. Pure deterministic. Returns stats.

    quick_refresh=True skips heat recomputation, promote/demote, and dashboards.
    Only scans arcs and regenerates brain-feed files. Target: <5ms.
    """
    now = datetime.now(timezone.utc)
    clusters_dir = vault_root / "clusters"
    conscious = vault_root / "frontal-lobe" / "conscious"
    unconscious = vault_root / "frontal-lobe" / "unconscious"

    # Phase 0: drain raw/inbox/ → region dirs before scanning arcs
    inbox_stats = drain_inbox(vault_root, dry_run=dry_run)

    # 1. Scan all arc files — region dirs first, then clusters/
    arcs: list[markers.DocumentMeta] = []
    arcs_by_date: dict[str, list[markers.DocumentMeta]] = {}
    seen_ids: set[str] = set()

    def _scan_and_collect(directory: Path, recurse: bool = False):
        """Scan .md files, parse, collect into arcs list. Deduplicates by stem."""
        if not directory.is_dir():
            return
        pattern = "**/*.md" if recurse else "*.md"
        files = list(sorted(directory.glob(pattern)))
        merged_stems = {
            f.name.replace(".merged.md", "") for f in files if f.name.endswith(".merged.md")
        }
        for md_file in files:
            if md_file.name.startswith("_"):
                continue
            stem = md_file.name[:-3]
            if not md_file.name.endswith(".merged.md") and stem in merged_stems:
                continue
            arc_id = md_file.stem.replace(".merged", "")
            if arc_id in seen_ids:
                continue
            seen_ids.add(arc_id)
            try:
                doc = markers.extract_all(md_file)
                arcs.append(doc)
            except Exception as e:
                print(f"WARN: failed to parse {md_file}: {e}", file=sys.stderr)

    # Region dirs (authoritative — promoted/graduated arcs)
    for region in REGION_DIRS:
        _scan_and_collect(vault_root / region, recurse=True)

    # Clusters dir (date-bucketed raw arcs — only if not already seen in a region)
    if clusters_dir.is_dir():
        for date_dir in sorted(clusters_dir.iterdir()):
            if not date_dir.is_dir():
                continue
            before = len(arcs)
            _scan_and_collect(date_dir)
            arcs_by_date[date_dir.name] = arcs[before:]

    # 2a. Build replay-edge boost map: count recent (<14d) arcs referencing
    #     each arc via `replayed_from`. Used by compute_heat below.
    replay_boost_map: dict[str, int] = {}
    if not quick_refresh:
        cutoff = now - timedelta(days=14)
        for arc in arcs:
            referenced = arc.frontmatter.get("replayed_from", "")
            if not referenced:
                continue
            created_str = arc.frontmatter.get("created", "")
            if created_str:
                try:
                    c = datetime.fromisoformat(created_str.replace("Z", "+00:00"))
                    if c.tzinfo is None:
                        c = c.replace(tzinfo=timezone.utc)
                    if c < cutoff:
                        continue
                except (ValueError, TypeError):
                    pass
            replay_boost_map[referenced] = replay_boost_map.get(referenced, 0) + 1

    # 2. Recompute heat (skipped in quick_refresh)
    heat_changes = 0
    if not quick_refresh:
        for arc in arcs:
            cid = arc.frontmatter.get("cluster_id", "")
            boost = replay_boost_map.get(cid, 0)
            old_heat = arc.frontmatter.get("heat", "0")
            new_heat = compute_heat(arc, now, replay_boost=boost)
            if str(new_heat) != str(old_heat):
                arc.frontmatter["heat"] = str(new_heat)
                heat_changes += 1
                if not dry_run and arc.path:
                    _update_frontmatter_heat(arc.path, new_heat)

    # 3. Promote/demote (skipped in quick_refresh)
    promotions = 0
    demotions = 0
    if not quick_refresh:
        for arc in arcs:
            heat = int(arc.frontmatter.get("heat", 0))
            if arc.path is None:
                continue
            fname = arc.path.name

            if heat >= BRAIN_PROMOTE_HEAT:
                dest = conscious / fname
                if arc.path.resolve() != dest.resolve():
                    if not dry_run:
                        conscious.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(str(arc.path), str(dest))
                    promotions += 1
            elif heat < BRAIN_DEMOTE_HEAT:
                conscious_file = conscious / fname
                if conscious_file.exists():
                    dest = unconscious / fname
                    if conscious_file.resolve() != dest.resolve():
                        if not dry_run:
                            unconscious.mkdir(parents=True, exist_ok=True)
                            shutil.move(str(conscious_file), str(dest))
                        demotions += 1

    # 3a. Extract workflow templates for hot reproducible arcs (skipped in quick_refresh)
    templates_written = 0
    if not quick_refresh:
        try:
            from extract_workflow import extract_workflow, format_markdown
        except ImportError:
            extract_workflow = None  # type: ignore
        if extract_workflow is not None:
            for arc in arcs:
                if arc.path is None:
                    continue
                heat = int(arc.frontmatter.get("heat", 0))
                status = arc.frontmatter.get("status", "")
                if heat < 4 or status != "active":
                    continue
                if arc.frontmatter.get("workflow_template") == "false":
                    continue
                if "## Workflow Template" in arc.path.read_text(encoding="utf-8"):
                    continue
                source_sessions = arc.frontmatter.get("source_sessions") or []
                if isinstance(source_sessions, str):
                    source_sessions = [source_sessions]
                if not source_sessions:
                    continue
                try:
                    steps = extract_workflow(source_sessions[0], max_steps=30)
                except (FileNotFoundError, RuntimeError):
                    continue
                if len(steps) < 3:
                    continue
                if not dry_run:
                    body = arc.path.read_text(encoding="utf-8")
                    template_section = (
                        "\n## Workflow Template\n\n"
                        f"{format_markdown(steps)}\n"
                    )
                    arc.path.write_text(body.rstrip() + "\n" + template_section,
                                        encoding="utf-8")
                    arc.frontmatter["workflow_template"] = "true"
                    _update_frontmatter_field(arc.path, "workflow_template", "true")
                templates_written += 1

    # 3b. Graduate cold arcs (heat <= BRAIN_GRADUATE_HEAT, age > BRAIN_GRADUATE_AGE_DAYS)
    # to hemisphere (skipped in quick_refresh).
    graduations = 0
    if not quick_refresh:
        for arc in arcs:
            heat = int(arc.frontmatter.get("heat", 0))
            if heat > BRAIN_GRADUATE_HEAT or arc.path is None:
                continue
            created = arc.frontmatter.get("created", "")
            if not created:
                continue
            try:
                created_dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
                if created_dt.tzinfo is None:
                    created_dt = created_dt.replace(tzinfo=timezone.utc)
                age_days = (now - created_dt).days
            except (ValueError, TypeError):
                continue
            if age_days <= BRAIN_GRADUATE_AGE_DAYS:
                continue
            region = arc.frontmatter.get("region", "left-hemisphere")
            region_map = {"left-hemisphere": "left", "right-hemisphere": "right",
                          "bridge": "bridge", "amygdala": "amygdala", "pineal": "pineal"}
            target_dir = vault_root / region_map.get(region, "left")
            dest = target_dir / arc.path.name
            if arc.path.resolve() != dest.resolve():
                if not dry_run:
                    target_dir.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(arc.path), str(dest))
                graduations += 1

    # 4. Build mitigation map. Any arc with status in {resolved, graduated}
    #    AND a `mitigates` frontmatter field tombstones signals whose source
    #    matches. Format: `mitigates: auth-broker` or `mitigates: [a, b, c]`.
    mitigated_sources: set[str] = set()
    for arc in arcs:
        fm = arc.frontmatter
        status = str(fm.get("status", "")).lower()
        if status not in {"resolved", "graduated"}:
            continue
        mitigates = fm.get("mitigates")
        if not mitigates:
            continue
        if isinstance(mitigates, str):
            mitigated_sources.add(mitigates.strip())
        elif isinstance(mitigates, list):
            for m in mitigates:
                if isinstance(m, str):
                    mitigated_sources.add(m.strip())

    # 5. Collect markers across all arcs. Tag each signal with its parent arc's
    #    `created` timestamp so the stale sweep in write_signals_feed can filter.
    #    Also tag with mitigation status when source matches mitigated_sources.
    all_signals: list[markers.Marker] = []
    all_injects: list[markers.Marker] = []
    all_lessons: list[markers.Marker] = []
    # Dedup by (source, content-hash). Two arcs carrying the same @signal marker
    # (same source, same claim) collapse to one bullet in signals.md.
    seen_signals: set[tuple[str, str]] = set()
    signals_deduped = 0
    for arc in arcs:
        arc_created = arc.frontmatter.get("created", "")
        for sig in arc.signals:
            sig.attrs["_parent_arc_created"] = arc_created
            sig_source = sig.attr("source", "")
            if sig_source and sig_source in mitigated_sources:
                sig.attrs["_mitigated"] = "true"
            content_hash = hashlib.sha256(sig.content.strip().encode("utf-8")).hexdigest()[:16]
            dedup_key = (sig_source, content_hash)
            if dedup_key in seen_signals:
                signals_deduped += 1
                continue
            seen_signals.add(dedup_key)
            all_signals.append(sig)
        all_injects.extend(arc.inject_blocks)
        all_lessons.extend(arc.lessons)

    # 5. Generate brain-feed outputs
    hot = sorted(arcs, key=lambda a: int(a.frontmatter.get("heat", 0)), reverse=True)[:10]
    signal_stats = {"written": 0, "tombstoned_stale": 0, "tombstoned_cleared": 0, "tombstoned_mitigated": 0}
    # Auto-verifier: run each signal's verify= command, tag _mitigated=true on
    # signals whose underlying claim has been falsified. write_signals_feed
    # already honors _mitigated for tombstoning, so no further plumbing needed.
    verify_results = brain_verifier.verify_all(all_signals)
    verify_stats = brain_verifier.apply_verify_results(all_signals, verify_results)
    if not dry_run:
        brain_feed_dir.mkdir(parents=True, exist_ok=True)
        write_hot_arcs_md(brain_feed_dir / "hot-arcs.md", hot)
        signal_stats = write_signals_feed(brain_feed_dir / "signals.md", all_signals, now=now)
        write_inject_feed(brain_feed_dir / "inject.md", all_injects)

    # 6. Update dashboards (skipped in quick_refresh)
    if not dry_run and not quick_refresh:
        for date_str, date_arcs in arcs_by_date.items():
            date_dir = clusters_dir / date_str
            try:
                os.chmod(str(date_dir), 0o777)
            except OSError:
                pass
            update_dashboard(date_dir, date_arcs)

    stats = {
        "inbox_drained": inbox_stats.get("drained", 0),
        "inbox_errors": inbox_stats.get("errors", 0),
        "arcs_scanned": len(arcs),
        "heat_changes": heat_changes,
        "promotions": promotions,
        "demotions": demotions,
        "templates_written": templates_written if not quick_refresh else 0,
        "graduations": graduations,
        "hot_arcs_written": len(hot),
        "signals_collected": len(all_signals),
        "signals_written": signal_stats["written"],
        "signals_tombstoned_stale": signal_stats["tombstoned_stale"],
        "signals_tombstoned_cleared": signal_stats["tombstoned_cleared"],
        "signals_tombstoned_mitigated": signal_stats["tombstoned_mitigated"],
        "signals_deduped": signals_deduped,
        "signals_verified_pass": verify_stats.get("verified_pass", 0),
        "signals_verified_fail": verify_stats.get("verified_fail", 0),
        "signals_verified_skip": verify_stats.get("verified_skip", 0),
        "signals_verified_error": verify_stats.get("verified_error", 0),
        "inject_blocks_collected": len(all_injects),
        "lessons_collected": len(all_lessons),
        "dry_run": dry_run,
    }
    return stats


def _update_frontmatter_heat(filepath: Path, new_heat: int) -> None:
    """Update the heat field in a file's YAML frontmatter."""
    text = filepath.read_text(encoding="utf-8")
    import re as _re
    updated = _re.sub(
        r'^(heat:\s*).*$',
        f'heat: {new_heat}',
        text,
        count=1,
        flags=_re.MULTILINE,
    )
    if updated != text:
        filepath.write_text(updated, encoding="utf-8")


def _update_frontmatter_field(filepath: Path, key: str, value: str) -> None:
    """Set or insert a scalar field in a file's YAML frontmatter."""
    import re as _re
    text = filepath.read_text(encoding="utf-8")
    pat = _re.compile(rf'^({_re.escape(key)}:\s*).*$', _re.MULTILINE)
    if pat.search(text):
        updated = pat.sub(f'{key}: {value}', text, count=1)
    else:
        m = _re.match(r'^---\n(.*?)\n---\n', text, _re.DOTALL)
        if not m:
            return
        fm_body = m.group(1)
        new_fm = f'---\n{fm_body}\n{key}: {value}\n---\n'
        updated = new_fm + text[m.end():]
    if updated != text:
        filepath.write_text(updated, encoding="utf-8")


# ── CLI ───────────────────────────────────────────────────────────────

def main() -> int:
    p = argparse.ArgumentParser(description="Deterministic brain-keeper maintenance tick")
    p.add_argument("--vault", required=True, help="Vault root path (e.g. /vault)")
    p.add_argument("--brain-feed", required=True, help="Brain feed output directory")
    p.add_argument("--dry-run", action="store_true", help="Print what would happen without writing")
    p.add_argument("--quick-refresh", action="store_true",
                   help="Skip heat/promote/demote. Only scan + write brain-feed outputs (<5ms)")
    args = p.parse_args()

    vault = Path(args.vault)
    feed = Path(args.brain_feed)

    stats = tick(vault, feed, dry_run=args.dry_run, quick_refresh=args.quick_refresh)

    json.dump(stats, sys.stdout, indent=2)
    sys.stdout.write("\n")

    if "error" in stats:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
