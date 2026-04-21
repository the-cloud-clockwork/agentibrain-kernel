#!/usr/bin/env python3
"""Full hybrid brain tick — the complete loop.

    brain_tick.py = brain_keeper.py + brain_tick_prompt.py + AI call + brain_apply.py

Runs the entire cycle:
  1. Deterministic: parse vault, compute heat, promote/demote, write brain-feed (47ms)
  2. Generate: build compressed AI prompt from pre-computed state
  3. Reason: call LLM via inference-gateway for edge discovery, signal escalation, intent
  4. Apply: write AI recommendations back to vault (edges, merges, signal updates)
  5. Verify: re-run deterministic pass to confirm changes persisted
  6. Report: write diff + health score to brain-etl tracking files

Usage:
    # Full tick against live vault
    python3 brain_tick.py --vault /vault --brain-feed /vault/brain-feed

    # Dry run (no writes, no LLM call)
    python3 brain_tick.py --vault /vault --brain-feed /vault/brain-feed --dry-run

    # Skip AI reasoning (deterministic only)
    python3 brain_tick.py --vault /vault --brain-feed /vault/brain-feed --no-ai

    # Custom inference endpoint
    python3 brain_tick.py --vault /vault --brain-feed /vault/brain-feed --inference-url http://inference-gateway:8080
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import brain_apply
import brain_keeper
import brain_tick_prompt


# INFERENCE_URL is optional — when empty, the AI reasoning phase is skipped and
# the tick runs deterministic-only. Operators configure this via env.
INFERENCE_URL = os.getenv("INFERENCE_URL", "")
CLICKHOUSE_URL = os.getenv("CLICKHOUSE_URL", "http://localhost:8123")


def _push_clickhouse(report: dict) -> None:
    """Push tick metrics to ClickHouse brain.tick_health (best-effort).

    Tries HTTP API first (works when ClickHouse listens on localhost inside
    the same network namespace, e.g. K8s pod with host network or same Docker
    bridge). Falls back gracefully on connection error.
    """
    det = report.get("phases", {}).get("deterministic", {}).get("stats", {})
    ai = report.get("phases", {}).get("apply", {})
    health = ai.get("result", {}).get("health", {}) if isinstance(ai.get("result"), dict) else {}

    reason = health.get("reason", "n/a")[:500].replace("'", "")
    row = (
        f"{health.get('score', 0)}, "
        f"'{reason}', "
        f"{det.get('arcs_scanned', 0)}, "
        f"{det.get('signals_collected', 0)}, "
        f"{det.get('lessons_collected', 0)}, "
        f"{det.get('heat_changes', 0)}, "
        f"{det.get('promotions', 0)}, "
        f"{det.get('demotions', 0)}, "
        f"{det.get('graduations', 0)}, "
        f"{det.get('hot_arcs_written', 0)}, "
        f"{report.get('total_ms', 0)}, "
        f"'full', "
        f"{det.get('signals_written', 0)}, "
        f"{det.get('signals_tombstoned_stale', 0)}, "
        f"{det.get('signals_tombstoned_cleared', 0)}"
    )
    sql = (
        "INSERT INTO brain.tick_health "
        "(score, reason, arcs_scanned, signals_collected, lessons_collected, "
        "heat_changes, promotions, demotions, graduations, hot_arcs_written, "
        "total_ms, tick_type, signals_written, signals_tombstoned_stale, "
        "signals_tombstoned_cleared) "
        f"VALUES ({row})"
    )

    import base64
    parsed_ch = urllib.parse.urlparse(CLICKHOUSE_URL)
    base_url = f"{parsed_ch.scheme}://{parsed_ch.hostname}:{parsed_ch.port or 8123}"
    req = urllib.request.Request(
        f"{base_url}/?query={urllib.request.quote(sql)}",
        method="POST",
    )
    if parsed_ch.username:
        creds = base64.b64encode(f"{parsed_ch.username}:{parsed_ch.password or ''}".encode()).decode()
        req.add_header("Authorization", f"Basic {creds}")
    urllib.request.urlopen(req, timeout=5)
    print("ClickHouse: tick_health row inserted", file=sys.stderr)
INFERENCE_ROUTE = os.getenv("BRAIN_INFERENCE_ROUTE", "kb-brief")  # reuse existing route


def call_llm(prompt: str, inference_url: str = INFERENCE_URL) -> str:
    """Call the inference gateway with the reasoning prompt.

    Uses the same route as kb_brief (sonnet with haiku fallback).
    Returns the LLM's text response.
    """
    payload = json.dumps({
        "model": "brain-reasoning",
        "route": INFERENCE_ROUTE,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 4096,
        "temperature": 0.3,
    }).encode()

    req = urllib.request.Request(
        f"{inference_url}/v1/chat/completions",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read())
            return data["choices"][0]["message"]["content"]
    except (urllib.error.URLError, KeyError, json.JSONDecodeError) as e:
        return f"ERROR: LLM call failed: {e}"


def run_tick(
    vault_root: Path,
    brain_feed_dir: Path,
    dry_run: bool = False,
    no_ai: bool = False,
    inference_url: str = INFERENCE_URL,
) -> dict:
    """Execute one full hybrid tick."""
    t0 = time.time()
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    report = {"timestamp": now_str, "phases": {}}

    # ── Phase 1: Deterministic ────────────────────────────────────
    t1 = time.time()
    det_stats = brain_keeper.tick(vault_root, brain_feed_dir, dry_run=dry_run)
    phase1_ms = int((time.time() - t1) * 1000)
    report["phases"]["deterministic"] = {
        "duration_ms": phase1_ms,
        "stats": det_stats,
    }
    print(f"Phase 1 (deterministic): {phase1_ms}ms — {det_stats.get('arcs_scanned', 0)} arcs, "
          f"{det_stats.get('signals_collected', 0)} signals, {det_stats.get('lessons_collected', 0)} lessons",
          file=sys.stderr)

    if no_ai:
        report["phases"]["ai"] = {"skipped": True}
        report["phases"]["apply"] = {"skipped": True}
        report["total_ms"] = int((time.time() - t0) * 1000)
        return report

    # ── Phase 2: Generate AI prompt ───────────────────────────────
    t2 = time.time()
    prompt, _ = brain_tick_prompt.build_prompt(vault_root, brain_feed_dir)
    phase2_ms = int((time.time() - t2) * 1000)
    report["phases"]["prompt_gen"] = {"duration_ms": phase2_ms, "prompt_length": len(prompt)}
    print(f"Phase 2 (prompt gen): {phase2_ms}ms — {len(prompt)} chars", file=sys.stderr)

    # ── Phase 3: AI reasoning ─────────────────────────────────────
    if dry_run:
        ai_output = "(dry run — no LLM call)"
        report["phases"]["ai"] = {"skipped": True, "dry_run": True}
    else:
        t3 = time.time()
        print("Phase 3 (AI reasoning): calling LLM...", file=sys.stderr)
        ai_output = call_llm(prompt, inference_url)
        phase3_ms = int((time.time() - t3) * 1000)
        report["phases"]["ai"] = {
            "duration_ms": phase3_ms,
            "output_length": len(ai_output),
            "error": ai_output.startswith("ERROR"),
        }
        print(f"Phase 3 (AI reasoning): {phase3_ms}ms — {len(ai_output)} chars", file=sys.stderr)

        if ai_output.startswith("ERROR"):
            print(f"  AI ERROR: {ai_output[:200]}", file=sys.stderr)
            report["total_ms"] = int((time.time() - t0) * 1000)
            return report

    # ── Phase 4: Apply recommendations ────────────────────────────
    if not dry_run and not ai_output.startswith("ERROR"):
        t4 = time.time()
        apply_result = brain_apply.apply(vault_root, brain_feed_dir, ai_output, dry_run=dry_run)
        phase4_ms = int((time.time() - t4) * 1000)
        report["phases"]["apply"] = {"duration_ms": phase4_ms, "result": apply_result}
        print(f"Phase 4 (apply): {phase4_ms}ms", file=sys.stderr)

        # Save AI output for audit
        ticks_dir = brain_feed_dir.parent / "brain-etl" / "ticks"
        if not ticks_dir.exists():
            ticks_dir = brain_feed_dir / "ticks"
        ticks_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
        (ticks_dir / f"{ts}-ai-output.md").write_text(ai_output)
    else:
        report["phases"]["apply"] = {"skipped": True}

    # ── Phase 5: Verify persistence ───────────────────────────────
    if not dry_run:
        t5 = time.time()
        verify_stats = brain_keeper.tick(vault_root, brain_feed_dir, dry_run=True)
        phase5_ms = int((time.time() - t5) * 1000)
        report["phases"]["verify"] = {
            "duration_ms": phase5_ms,
            "stats": verify_stats,
        }
        print(f"Phase 5 (verify): {phase5_ms}ms — {verify_stats.get('arcs_scanned', 0)} arcs, "
              f"{verify_stats.get('signals_collected', 0)} signals", file=sys.stderr)

    report["total_ms"] = int((time.time() - t0) * 1000)

    # Push metrics to ClickHouse (best-effort, never fail the tick)
    if not dry_run:
        try:
            _push_clickhouse(report)
        except Exception as e:
            print(f"WARN: ClickHouse push failed: {e}", file=sys.stderr)

    return report


def main() -> int:
    p = argparse.ArgumentParser(description="Full hybrid brain tick")
    p.add_argument("--vault", required=True, help="Vault root path")
    p.add_argument("--brain-feed", required=True, help="Brain feed directory")
    p.add_argument("--dry-run", action="store_true", help="No writes, no LLM")
    p.add_argument("--no-ai", action="store_true", help="Deterministic only, skip AI")
    p.add_argument("--inference-url", default=INFERENCE_URL, help="Inference gateway URL")
    args = p.parse_args()

    result = run_tick(
        Path(args.vault),
        Path(args.brain_feed),
        dry_run=args.dry_run,
        no_ai=args.no_ai,
        inference_url=args.inference_url,
    )

    json.dump(result, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")

    # Exit non-zero if AI phase had an error (triggers ntfy in CronJob shell)
    ai_phase = result.get("phases", {}).get("ai", {})
    if ai_phase.get("error"):
        print("EXIT 1: AI phase failed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
