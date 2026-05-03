#!/usr/bin/env python3
"""Amygdala — Redis Streams consumer for emergency signal propagation.

Reads events from the Redis event bus (DB 11), classifies severity
deterministically, and writes signal files to brain-feed/ NFS.

The signal file (amygdala-active.md) uses brain_adapter frontmatter so
agents pick it up via the existing broadcast system. File presence = alert
active. File absence = all clear.

Usage:
    # One-shot check (run from brain-cron)
    python3 amygdala.py --redis-url redis://redis:6379/11 \
        --vault /vault --brain-feed /vault/brain-feed

    # Dry run
    python3 amygdala.py --redis-url redis://redis:6379/11 \
        --vault /vault --brain-feed /vault/brain-feed --dry-run
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    import redis
except ImportError:
    redis = None

_DEFAULT_STREAMS = "events:health,events:host,events:deploy,events:system,events:brain"
# Operators with an existing namespaced prefix (e.g. <prefix>:events:*) override via
# AMYGDALA_STREAMS="<prefix>:events:health,<prefix>:events:host,...".
STREAMS = [s.strip() for s in os.getenv("AMYGDALA_STREAMS", _DEFAULT_STREAMS).split(",") if s.strip()]
GROUP = "amygdala"
CONSUMER = os.getenv("AMYGDALA_CONSUMER", "amygdala-cron")
CLEAR_WINDOW_SEC = int(os.getenv("AMYGDALA_CLEAR_WINDOW", "900"))  # 15 min


def classify_severity(fields: dict) -> str | None:
    """Deterministic severity classification from event fields."""
    priority = fields.get("priority", "default")
    event = fields.get("event", "")
    message = fields.get("message", "").lower()
    title = fields.get("title", "").lower()
    text = f"{event} {message} {title}"

    # Brain-sourced events carry their own severity
    if event.startswith("brain."):
        sev = fields.get("severity", "")
        return sev if sev in ("nuclear", "critical", "warning") else None

    if priority == "urgent" or any(w in text for w in ["down", "offline", "fatal", "data loss", "nuclear"]):
        return "nuclear"
    if priority == "high" or any(w in text for w in ["failed", "crash", "timeout", "unreachable"]):
        return "critical"
    if any(w in text for w in ["warning", "threshold", "degraded", "slow"]):
        return "warning"
    return None  # not amygdala-worthy


def write_signal_file(brain_feed_dir: Path, events: list[dict], dry_run: bool) -> bool:
    """Write amygdala-active.md with the highest-severity active signal."""
    if not events:
        return False

    worst = max(events, key=lambda e: {"nuclear": 3, "critical": 2, "warning": 1}.get(e["severity"], 0))
    sev = worst["severity"]
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    lines = [
        "---",
        "id: amygdala-active",
        "title: AMYGDALA ALERT",
        "priority: 100",
        "ttl: 300",
        f"severity: {'critical' if sev in ('nuclear', 'critical') else 'alert'}",
        "---",
        "",
        f"## [{sev.upper()}] {worst['title']}",
        "",
        f"Source: {worst['stream']} | event: {worst['event']}",
        f"Time: {now_str}",
        f"Affected: {worst.get('source', 'unknown')}",
        "",
    ]

    if sev == "nuclear":
        lines.append("**ACTION REQUIRED: Halt non-critical operations. Operator notified.**")
    elif sev == "critical":
        lines.append("**CAUTION: Monitor closely. Escalation possible.**")

    if len(events) > 1:
        lines.extend(["", f"### Active signals ({len(events)} total)", ""])
        for ev in events:
            lines.append(f"- [{ev['severity']}] {ev['title']} ({ev['stream']})")

    content = "\n".join(lines) + "\n"

    if not dry_run:
        brain_feed_dir.mkdir(parents=True, exist_ok=True)
        (brain_feed_dir / "amygdala-active.md").write_text(content)
    print(f"AMYGDALA: wrote signal file — {sev} — {worst['title']}", file=sys.stderr)
    return True


def write_incident_arc(vault_root: Path, event: dict, dry_run: bool) -> Path | None:
    """Create an incident arc in amygdala/ vault folder."""
    amygdala_dir = vault_root / "amygdala"
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    slug = event.get("event", "unknown").replace(".", "-")
    filename = f"{ts}-{slug}.md"

    content = f"""---
cluster_id: amygdala-{ts}-{slug}
title: "{event['title']}"
region: amygdala
status: active
heat: 10
severity: {event['severity']}
source_event: "{event['event']}"
source_stream: "{event['stream']}"
created: {datetime.now(timezone.utc).strftime('%Y-%m-%d')}
---

# {event['title']}

<!-- @signal severity={event['severity']} source={event.get('source', 'event-bus')} -->
{event.get('message', event['title'])}
<!-- @/signal -->

## Timeline

- **{ts}** — Signal detected from {event['stream']}

## Resolution

(pending)
"""
    if not dry_run:
        amygdala_dir.mkdir(parents=True, exist_ok=True)
        path = amygdala_dir / filename
        path.write_text(content)
        return path
    return None


def clear_signal(brain_feed_dir: Path, dry_run: bool) -> bool:
    """Remove amygdala-active.md when all clear."""
    signal_file = brain_feed_dir / "amygdala-active.md"
    if signal_file.exists():
        if not dry_run:
            signal_file.unlink()
        print("AMYGDALA: cleared — no active signals", file=sys.stderr)
        return True
    return False


def consume(redis_url: str, vault_root: Path, brain_feed_dir: Path, dry_run: bool = False) -> dict:
    """One-shot consume: read pending events, classify, write signals."""
    if redis is None:
        return {"error": "redis package not installed"}

    r = redis.Redis.from_url(redis_url, decode_responses=True)
    last_event_key = "amygdala:last_event_ts"

    # Ensure consumer groups exist
    for stream in STREAMS:
        try:
            r.xgroup_create(stream, GROUP, id="0", mkstream=True)
        except redis.exceptions.ResponseError as e:
            if "BUSYGROUP" not in str(e):
                raise

    # Read pending + new
    streams_map = {s: ">" for s in STREAMS}
    results = r.xreadgroup(GROUP, CONSUMER, streams_map, count=50, block=2000)

    active_events = []
    for stream_name, messages in results:
        for msg_id, fields in messages:
            severity = classify_severity(fields)
            if severity:
                active_events.append({
                    "severity": severity,
                    "title": fields.get("title", fields.get("event", "unknown")),
                    "event": fields.get("event", "unknown"),
                    "message": fields.get("message", ""),
                    "source": fields.get("source", ""),
                    "stream": stream_name,
                    "msg_id": msg_id,
                    "ts": fields.get("ts", ""),
                })
            r.xack(stream_name, GROUP, msg_id)

    stats = {
        "events_read": sum(len(msgs) for _, msgs in results),
        "signals_detected": len(active_events),
    }

    if active_events:
        wrote = write_signal_file(brain_feed_dir, active_events, dry_run)
        stats["signal_file_written"] = wrote

        # Write incident arcs for nuclear/critical
        for ev in active_events:
            if ev["severity"] in ("nuclear", "critical"):
                write_incident_arc(vault_root, ev, dry_run)

        # Record last event timestamp
        if not dry_run:
            r.set(last_event_key, str(time.time()), ex=CLEAR_WINDOW_SEC * 2)
    else:
        # Check if we should clear — no events for CLEAR_WINDOW_SEC
        last_ts = r.get(last_event_key)
        if last_ts:
            elapsed = time.time() - float(last_ts)
            if elapsed >= CLEAR_WINDOW_SEC:
                cleared = clear_signal(brain_feed_dir, dry_run)
                stats["cleared"] = cleared
                if not dry_run:
                    r.delete(last_event_key)
        # If no last_event_key exists, do NOT clear — the signal file may have
        # been written by another process (brain-cron one-shot). Only clear
        # when we KNOW events existed and then stopped for CLEAR_WINDOW_SEC.

    return stats


def replay(redis_url: str, count: int = 100, severity_filter: str | None = None) -> dict:
    """Replay the last N events from all amygdala streams via XREVRANGE.

    Forensics tool: read-only. Does NOT write signal files, does NOT mark
    messages as consumed. Use after a crash or when investigating why a
    specific signal fired (or didn't).

    Returns a dict with per-stream counts and a flat list of events sorted
    newest-first, classified by severity.
    """
    if redis is None:
        return {"error": "redis package not installed"}

    r = redis.Redis.from_url(redis_url, decode_responses=True)
    all_events: list[dict] = []
    per_stream: dict[str, int] = {}

    for stream in STREAMS:
        try:
            # XREVRANGE = newest first; COUNT caps per stream
            entries = r.xrevrange(stream, count=count)
        except redis.exceptions.ResponseError:
            per_stream[stream] = 0
            continue

        per_stream[stream] = len(entries)
        for msg_id, fields in entries:
            severity = classify_severity(fields) or "info"
            if severity_filter and severity != severity_filter:
                continue
            all_events.append({
                "stream": stream,
                "msg_id": msg_id,
                "severity": severity,
                "event": fields.get("event", "unknown"),
                "title": fields.get("title", "")[:120],
                "message": fields.get("message", "")[:200],
                "source": fields.get("source", ""),
                "ts": fields.get("ts", ""),
            })

    # Sort newest-first across all streams (msg_id is timestamp-based)
    all_events.sort(key=lambda e: e["msg_id"], reverse=True)
    all_events = all_events[:count]

    by_severity: dict[str, int] = {}
    for ev in all_events:
        by_severity[ev["severity"]] = by_severity.get(ev["severity"], 0) + 1

    return {
        "mode": "replay",
        "count_requested": count,
        "count_returned": len(all_events),
        "severity_filter": severity_filter,
        "per_stream": per_stream,
        "by_severity": by_severity,
        "events": all_events,
    }


def run_continuous(redis_url: str, vault_root: Path, brain_feed_dir: Path, poll_interval: int = 5):
    """Continuous consumer loop. Blocks on XREADGROUP, checks every poll_interval seconds."""
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)
    print(f"Amygdala continuous mode: polling every {poll_interval}s", flush=True)
    print(f"  redis={redis_url[:40]}... vault={vault_root} feed={brain_feed_dir}", flush=True)
    cycle = 0
    while True:
        try:
            stats = consume(redis_url, vault_root, brain_feed_dir)
            cycle += 1
            if stats.get("signals_detected", 0) > 0 or stats.get("cleared"):
                print(f"[cycle {cycle}] {json.dumps(stats)}", flush=True)
            elif cycle % 60 == 0:
                print(f"[cycle {cycle}] heartbeat — no signals", flush=True)
        except KeyboardInterrupt:
            print("Amygdala: shutdown", flush=True)
            break
        except Exception as e:
            import traceback
            print(f"Amygdala error (cycle {cycle}): {e}", flush=True)
            traceback.print_exc()
            time.sleep(30)


def main() -> int:
    p = argparse.ArgumentParser(description="Amygdala — Redis Streams emergency signal consumer")
    p.add_argument("--redis-url", default=os.getenv("REDIS_URL", "redis://redis:6379/11"))
    p.add_argument("--vault", help="Vault root path (required unless --replay)")
    p.add_argument("--brain-feed", help="Brain feed directory (required unless --replay)")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--continuous", action="store_true", help="Run as continuous consumer (daemon mode)")
    p.add_argument("--poll-interval", type=int, default=5, help="Seconds between polls in continuous mode")
    p.add_argument("--replay", action="store_true", help="Forensics: replay last N events from all streams (read-only, no side effects)")
    p.add_argument("--last", type=int, default=100, help="Number of events to replay (default 100, max 1000)")
    p.add_argument("--severity", choices=["info", "warning", "critical", "nuclear"], help="Filter replay by severity")
    args = p.parse_args()

    if args.replay:
        count = max(1, min(args.last, 1000))
        result = replay(args.redis_url, count=count, severity_filter=args.severity)
        json.dump(result, sys.stdout, indent=2, default=str)
        sys.stdout.write("\n")
        return 0

    if not args.vault or not args.brain_feed:
        print("ERROR: --vault and --brain-feed are required unless --replay is set", file=sys.stderr)
        return 2

    if args.continuous:
        run_continuous(args.redis_url, Path(args.vault), Path(args.brain_feed), args.poll_interval)
        return 0

    result = consume(args.redis_url, Path(args.vault), Path(args.brain_feed), args.dry_run)
    json.dump(result, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
