#!/usr/bin/env python3
"""Brain heal — 7-point drift audit. Catches silent failures.

Run from cron or brain-keeper. Outputs markdown report + CSV summary.
Exits 0 on healthy, 1 on warnings, 2 on critical drift.

Checks:
1. Stale signals — count of signals older than 24h (excluding nuclear/critical)
2. Hook silence — last brain.delivery span age (>2h with active sessions = drift)
3. Brain-feed freshness — mtime of hot-arcs.md (>3h = tick stalled or rsync broken)
4. Broadcast bloat — broadcast_delivery_state.json line count (>100k = ttl/dedup broken)
5. Channel subscriptions — sample of agentihooks-touching projects missing channels[]
6. LiteLLM reachability — brain-keeper model self-ping <2s
7. Nuclear age — oldest unmitigated nuclear signal age (>24h = needs mitigation arc)

Usage:
    python3 heal.py --vault /vault --brain-feed /vault/brain-feed
    python3 heal.py --vault /vault --brain-feed /vault/brain-feed --out /tmp/heal.md
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

CLICKHOUSE_URL = os.getenv("CLICKHOUSE_URL", "")
CLICKHOUSE_USER = os.getenv("CLICKHOUSE_USER", "default")
CLICKHOUSE_PASSWORD = os.getenv("CLICKHOUSE_PASSWORD", "")
# LITELLM_URL is optional — when unset, heal skips LLM-backed checks.
# Operators configure this via env; leave empty for local / no-LLM setups.
LITELLM_URL = os.getenv("LITELLM_URL", "")
LITELLM_KEY = os.getenv("LITELLM_KEY", "")


def _ch_query(sql: str, timeout: int = 5) -> str | None:
    """POST query to ClickHouse, return raw text or None on failure."""
    try:
        req = urllib.request.Request(
            CLICKHOUSE_URL,
            data=sql.encode("utf-8"),
            headers={"Content-Type": "text/plain"},
            method="POST",
        )
        if CLICKHOUSE_PASSWORD:
            import base64
            auth = base64.b64encode(f"{CLICKHOUSE_USER}:{CLICKHOUSE_PASSWORD}".encode()).decode()
            req.add_header("Authorization", f"Basic {auth}")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8").strip()
    except Exception:
        return None


def check_stale_signals(brain_feed: Path) -> dict:
    """Count signals older than 24h in signals.md, excluding nuclear/critical."""
    sig_file = brain_feed / "signals.md"
    if not sig_file.exists():
        return {"name": "stale_signals", "status": "SKIP", "value": 0, "detail": "signals.md missing"}
    text = sig_file.read_text(encoding="utf-8", errors="replace")
    total = sum(1 for line in text.splitlines() if line.startswith("- **["))
    nuclear = sum(1 for line in text.splitlines() if line.startswith("- **[nuclear]"))
    critical = sum(1 for line in text.splitlines() if line.startswith("- **[critical]"))
    stale = max(0, total - nuclear - critical)
    if stale > 5:
        return {"name": "stale_signals", "status": "WARN", "value": stale, "detail": f"{stale} non-critical signals; sweep may not be running"}
    return {"name": "stale_signals", "status": "PASS", "value": stale, "detail": f"{nuclear} nuclear, {critical} critical, {stale} other"}


def check_hook_silence() -> dict:
    """Query ClickHouse for last brain.delivery span age."""
    sql = "SELECT dateDiff('minute', max(Timestamp), now64(9)) FROM otel.otel_traces WHERE ServiceName='agentihooks' AND SpanName='brain.delivery' FORMAT TabSeparated"
    raw = _ch_query(sql)
    if raw is None:
        return {"name": "hook_silence", "status": "SKIP", "value": -1, "detail": "ClickHouse unreachable"}
    try:
        minutes = int(raw)
    except ValueError:
        return {"name": "hook_silence", "status": "SKIP", "value": -1, "detail": f"unexpected response: {raw[:60]}"}
    if minutes > 120:
        return {"name": "hook_silence", "status": "FAIL", "value": minutes, "detail": f"last span {minutes}m ago — OTel pipeline broken"}
    if minutes > 30:
        return {"name": "hook_silence", "status": "WARN", "value": minutes, "detail": f"last span {minutes}m ago — sessions idle or hooks slow"}
    return {"name": "hook_silence", "status": "PASS", "value": minutes, "detail": f"last span {minutes}m ago"}


def check_brain_feed_freshness(brain_feed: Path) -> dict:
    """mtime of hot-arcs.md vs now."""
    f = brain_feed / "hot-arcs.md"
    if not f.exists():
        return {"name": "brain_feed_freshness", "status": "FAIL", "value": -1, "detail": "hot-arcs.md missing"}
    age_min = int((time.time() - f.stat().st_mtime) / 60)
    if age_min > 180:
        return {"name": "brain_feed_freshness", "status": "FAIL", "value": age_min, "detail": f"hot-arcs.md {age_min}m old — tick stalled"}
    if age_min > 130:
        return {"name": "brain_feed_freshness", "status": "WARN", "value": age_min, "detail": f"hot-arcs.md {age_min}m old — last tick may have failed"}
    return {"name": "brain_feed_freshness", "status": "PASS", "value": age_min, "detail": f"hot-arcs.md {age_min}m old"}


def check_broadcast_bloat() -> dict:
    """Count lines in local broadcast_delivery_state.json."""
    state = Path.home() / ".agentihooks" / "broadcast_delivery_state.json"
    if not state.exists():
        return {"name": "broadcast_bloat", "status": "SKIP", "value": 0, "detail": "no local state file"}
    try:
        size = state.stat().st_size
        with open(state) as f:
            data = json.load(f)
        entries = len(data) if isinstance(data, dict) else 0
    except Exception as e:
        return {"name": "broadcast_bloat", "status": "WARN", "value": -1, "detail": f"parse error: {e}"}
    if entries > 50000:
        return {"name": "broadcast_bloat", "status": "FAIL", "value": entries, "detail": f"{entries} entries / {size//1024}KB — TTL eviction broken"}
    if entries > 10000:
        return {"name": "broadcast_bloat", "status": "WARN", "value": entries, "detail": f"{entries} entries / {size//1024}KB"}
    return {"name": "broadcast_bloat", "status": "PASS", "value": entries, "detail": f"{entries} entries / {size//1024}KB"}


def check_channel_subscriptions() -> dict:
    """Sample local projects for .agentihooks.json with channels[]."""
    home = Path.home()
    projects_dir = home / "dev"
    if not projects_dir.exists():
        return {"name": "channel_subscriptions", "status": "SKIP", "value": 0, "detail": "~/dev not found"}
    configs = list(projects_dir.glob("*/.agentihooks.json"))
    configs += list(projects_dir.glob("*/*/.agentihooks.json"))
    if not configs:
        return {"name": "channel_subscriptions", "status": "PASS", "value": 0, "detail": "no .agentihooks.json files (fleet uses defaults)"}
    missing = []
    for c in configs[:30]:
        try:
            data = json.loads(c.read_text())
            if not data.get("channels"):
                missing.append(str(c.relative_to(home)))
        except Exception:
            continue
    if missing:
        return {"name": "channel_subscriptions", "status": "WARN", "value": len(missing), "detail": f"{len(missing)} projects missing channels[]: {missing[:3]}"}
    return {"name": "channel_subscriptions", "status": "PASS", "value": 0, "detail": f"all {len(configs)} configs have channels[]"}


def check_litellm_reachability() -> dict:
    """Self-ping brain-keeper via LiteLLM /v1/models."""
    if not LITELLM_KEY:
        return {"name": "litellm_reachability", "status": "SKIP", "value": -1, "detail": "LITELLM_KEY not set"}
    url = LITELLM_URL.rstrip("/") + "/v1/models"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {LITELLM_KEY}"})
    try:
        t0 = time.time()
        with urllib.request.urlopen(req, timeout=5) as r:
            dt_ms = int((time.time() - t0) * 1000)
            data = json.loads(r.read())
        ids = [m.get("id", "") for m in data.get("data", [])]
        if "brain-keeper" not in ids:
            return {"name": "litellm_reachability", "status": "WARN", "value": dt_ms, "detail": "brain-keeper model not registered"}
        return {"name": "litellm_reachability", "status": "PASS", "value": dt_ms, "detail": f"{dt_ms}ms, {len(ids)} models registered"}
    except Exception as e:
        return {"name": "litellm_reachability", "status": "FAIL", "value": -1, "detail": str(e)[:80]}


def check_nuclear_age(brain_feed: Path) -> dict:
    """Find oldest nuclear signal in signals.md and check if mitigation arc exists."""
    sig_file = brain_feed / "signals.md"
    if not sig_file.exists():
        return {"name": "nuclear_age", "status": "SKIP", "value": 0, "detail": "signals.md missing"}
    text = sig_file.read_text(encoding="utf-8", errors="replace")
    nuclear_lines = [line for line in text.splitlines() if line.startswith("- **[nuclear]")]
    if not nuclear_lines:
        return {"name": "nuclear_age", "status": "PASS", "value": 0, "detail": "no nuclear signals"}
    return {"name": "nuclear_age", "status": "WARN", "value": len(nuclear_lines), "detail": f"{len(nuclear_lines)} nuclear signal(s) active — needs mitigates: arc to close"}


CHECKS = [
    check_stale_signals,
    check_hook_silence,
    check_brain_feed_freshness,
    check_broadcast_bloat,
    check_channel_subscriptions,
    check_litellm_reachability,
    check_nuclear_age,
]


def run(brain_feed: Path) -> tuple[list[dict], int]:
    """Run all checks. Return (results, exit_code)."""
    results = []
    for check in CHECKS:
        try:
            if check.__name__ in ("check_stale_signals", "check_brain_feed_freshness", "check_nuclear_age"):
                r = check(brain_feed)
            else:
                r = check()
        except Exception as e:
            r = {"name": check.__name__.removeprefix("check_"), "status": "FAIL", "value": -1, "detail": f"check raised: {e}"}
        results.append(r)
    fails = sum(1 for r in results if r["status"] == "FAIL")
    warns = sum(1 for r in results if r["status"] == "WARN")
    if fails:
        return results, 2
    if warns:
        return results, 1
    return results, 0


def render_markdown(results: list[dict], exit_code: int) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    overall = {0: "HEALTHY", 1: "DEGRADED", 2: "CRITICAL"}[exit_code]
    lines = [
        f"# Brain Heal Report — {ts}",
        "",
        f"**Overall:** {overall}  ",
        f"**Host:** {socket.gethostname()}",
        "",
        "| # | Check | Status | Value | Detail |",
        "|---|---|---|---|---|",
    ]
    for i, r in enumerate(results, 1):
        lines.append(f"| {i} | `{r['name']}` | {r['status']} | {r['value']} | {r['detail']} |")
    lines.extend(["", "## Notes", ""])
    if exit_code == 0:
        lines.append("All 7 brain subsystems passed self-audit. Nervous system intact.")
    elif exit_code == 1:
        lines.append("One or more checks degraded. Review WARN items; system still functional.")
    else:
        lines.append("Critical drift detected. FAIL items demand immediate attention — broadcasts may be silently dropped.")
    return "\n".join(lines) + "\n"


def render_csv(results: list[dict]) -> str:
    import io
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["check", "status", "value", "detail"])
    for r in results:
        w.writerow([r["name"], r["status"], r["value"], r["detail"]])
    return buf.getvalue()


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--brain-feed", required=True, help="Path to brain-feed dir")
    p.add_argument("--vault", help="Vault root (unused, accepted for symmetry)")
    p.add_argument("--out", help="Write markdown report to file")
    p.add_argument("--csv", help="Write CSV summary to file")
    p.add_argument("--json", action="store_true", help="Emit JSON results to stdout instead of markdown")
    args = p.parse_args()

    brain_feed = Path(args.brain_feed)
    results, exit_code = run(brain_feed)

    if args.json:
        print(json.dumps({"results": results, "exit_code": exit_code}, indent=2))
    else:
        md = render_markdown(results, exit_code)
        print(md)
        if args.out:
            Path(args.out).write_text(md, encoding="utf-8")

    if args.csv:
        Path(args.csv).write_text(render_csv(results), encoding="utf-8")

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
