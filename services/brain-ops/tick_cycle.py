#!/usr/bin/env python3
"""One maintenance and indexing pipeline for the scheduled and requested ticks.

    tick_cycle.py drain --vault /vault       # requested ticks (tick-drain CronJob)
    tick_cycle.py scheduled --vault /vault   # scheduled tick (brain-ops CronJob)

The pipeline is brain_tick (maintenance), embed_arcs (arcs and lesson logs) and
embed_raw (raw notes). A request reaches ticks/completed/ only after every step
succeeded, so a completed tick certifies both text and semantic retrieval.

Each drain counts an attempt on a request before its cycle starts. An index
failure leaves the request in ticks/requested/ with maintenance recorded as
done and `last_error`; the next drain resumes it at indexing. A request whose
attempts reach TICK_CYCLE_MAX_ATTEMPTS, including cycles killed mid run, moves
to ticks/failed/. Dry runs never index.

TICK_CYCLE_MODE selects what the scheduled tick adds around the pipeline:
  vault        pure vault processing
  workstation  transcript extraction at EXTRACT_HOUR before, amygdala check after
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
MODES = ("vault", "workstation")
TAIL_CHARS = 2000
QUEUE = Path("brain-feed") / "ticks"


@dataclass(frozen=True)
class Step:
    ok: bool
    output: str


@dataclass(frozen=True)
class Request:
    path: Path
    data: dict


def _run(argv: list[str], stdin=None) -> Step:
    proc = subprocess.run(
        [sys.executable, *argv], stdin=stdin, capture_output=True, text=True, check=False
    )
    output = proc.stdout + proc.stderr
    sys.stdout.write(output)
    sys.stdout.flush()
    return Step(proc.returncode == 0, output)


def maintenance(vault: Path, flags: list[str], source: str) -> Step:
    inference_url = os.environ.get("INFERENCE_URL", "")
    no_ai = [] if inference_url or "--no-ai" in flags else ["--no-ai"]
    return _run(
        [
            str(HERE / "brain_tick.py"),
            "--vault",
            str(vault),
            "--brain-feed",
            str(vault / "brain-feed"),
            "--inference-url",
            inference_url,
            "--source",
            source,
            *flags,
            *no_ai,
        ]
    )


def index(vault: Path) -> Step:
    if not (os.environ.get("EMBEDDINGS_API_KEY") or os.environ.get("EMBED_API_KEY")):
        return Step(False, "EMBEDDINGS_API_KEY is not set, so the semantic index was not refreshed")
    outputs = []
    for script in ("embed_arcs.py", "embed_raw.py"):
        step = _run([str(HERE / script), "--vault", str(vault), "--prune"])
        outputs.append(step.output)
        if not step.ok:
            return Step(False, f"{script} failed\n" + "".join(outputs))
    return Step(True, "".join(outputs))


def pipeline(
    vault: Path,
    flags: list[str],
    source: str,
    resume: bool = False,
    before_index: Callable[[], None] | None = None,
) -> tuple[str, Step]:
    if resume:
        print("maintenance: resumed, already done for these requests")
    else:
        step = maintenance(vault, flags, source)
        if not step.ok:
            return "maintenance", step
    if "--dry-run" in flags:
        return "done", Step(True, "")
    if before_index:
        before_index()
    step = index(vault)
    return ("done" if step.ok else "index"), step


def _write(path: Path, data: dict) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def _finish(request: Request, data: dict, dest: Path) -> bool:
    try:
        _write(request.path, data)
        os.replace(request.path, dest / request.path.name)
    except OSError as exc:
        print(f"WARN: could not move {request.path.name} to {dest.name}: {exc}")
        return False
    return True


def _requeue(request: Request, data: dict) -> bool:
    try:
        _write(request.path, data)
    except OSError as exc:
        print(f"WARN: could not record the retry on {request.path.name}: {exc}")
        return False
    return True


def _attempts(data: dict) -> int:
    try:
        return int(data.get("attempts", 0))
    except (TypeError, ValueError):
        return 0


def _pending(requested: Path, failed: Path) -> dict[tuple[bool, bool, bool], list[Request]]:
    buckets: dict[tuple[bool, bool, bool], list[Request]] = {}
    for path in sorted(requested.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            reason = "" if isinstance(data, dict) else "not a JSON object"
        except (OSError, ValueError) as exc:
            reason = str(exc)
        if reason:
            _finish(Request(path, {}), {"error_tail": f"unreadable tick request: {reason}"}, failed)
            continue
        resumed = data.get("maintenance") == "done"
        kind = (bool(data.get("dry_run")), bool(data.get("no_ai")), resumed)
        buckets.setdefault(kind, []).append(Request(path, data))
    return buckets


def _start(requests: list[Request], max_attempts: int, failed: Path) -> tuple[list[Request], dict]:
    counts = {"completed": 0, "failed": 0, "retrying": 0, "stuck": 0}
    live = []
    for request in requests:
        attempts = _attempts(request.data)
        if attempts >= max_attempts:
            tail = request.data.get("last_error") or "the tick cycle never finished"
            saved = _finish(request, {**request.data, "error_tail": tail}, failed)
            counts["failed" if saved else "stuck"] += 1
            continue
        request.data["attempts"] = attempts + 1
        if _requeue(request, request.data):
            live.append(request)
        else:
            counts["stuck"] += 1
    return live, counts


def _drain_bucket(vault: Path, kind: tuple[bool, bool, bool], requests: list[Request]) -> dict:
    dirs = {name: vault / QUEUE / name for name in ("completed", "failed")}
    dry_run, no_ai, resume = kind
    flags = ["--dry-run"] * dry_run + ["--no-ai"] * no_ai
    max_attempts = int(os.environ.get("TICK_CYCLE_MAX_ATTEMPTS", "10"))
    live, counts = _start(requests, max_attempts, dirs["failed"])
    if not live:
        return counts

    def index_started() -> None:
        for request in live:
            request.data["maintenance"] = "done"
            _requeue(request, request.data)

    print(f"drain: {len(live)} request(s) in one cycle [flags='{' '.join(flags)}']")
    stage, step = pipeline(vault, flags, "brain-drain", resume, index_started)
    tail = step.output[-TAIL_CHARS:]
    for request in live:
        data = dict(request.data)
        outcome = "completed"
        if stage == "maintenance":
            data["error_tail"] = tail
            outcome = "failed"
        elif stage == "index":
            data["last_error"] = tail
            outcome = "failed" if _attempts(data) >= max_attempts else "retrying"
            if outcome == "failed":
                data["error_tail"] = tail
        if outcome == "retrying":
            saved = _requeue(request, data)
        else:
            saved = _finish(request, data, dirs[outcome])
        counts[outcome if saved else "stuck"] += 1
    return counts


def drain(vault: Path) -> int:
    requested, failed = vault / QUEUE / "requested", vault / QUEUE / "failed"
    for name in ("requested", "completed", "failed"):
        (vault / QUEUE / name).mkdir(parents=True, exist_ok=True)
    totals = {"completed": 0, "failed": 0, "retrying": 0, "stuck": 0}
    for kind, requests in sorted(_pending(requested, failed).items()):
        for key, value in _drain_bucket(vault, kind, requests).items():
            totals[key] += value
    print("drain-summary: " + " ".join(f"{k}={v}" for k, v in totals.items()))
    return 0 if totals["failed"] + totals["retrying"] + totals["stuck"] == 0 else 1


def extract(vault: Path) -> Step:
    hour = os.environ.get("EXTRACT_HOUR", "04")
    if time.strftime("%H", time.gmtime()) != hour:
        print(f"Skipping extraction (runs at {hour} UTC only).")
        return Step(True, "")
    outdir = vault / "clusters" / time.strftime("%Y-%m-%d", time.gmtime())
    outdir.mkdir(parents=True, exist_ok=True)
    outdir.chmod(0o777)
    producer = subprocess.Popen(
        [
            sys.executable,
            str(HERE / "extract.py"),
            "--since",
            os.environ.get("EXTRACT_SINCE", "26h"),
            "--min-turns",
            os.environ.get("EXTRACT_MIN_TURNS", "5"),
            "--projects-dir",
            os.environ.get("EXTRACT_PROJECTS_DIR", "/shared/.claude/projects"),
        ],
        stdout=subprocess.PIPE,
    )
    step = _run([str(HERE / "cluster.py"), "--out-dir", str(outdir)], stdin=producer.stdout)
    producer.stdout.close()
    return Step(producer.wait() == 0 and step.ok, step.output)


def amygdala(vault: Path) -> Step:
    print("Running amygdala check...")
    redis_url = os.environ.get("REDIS_URL", "").rsplit("/", 1)[0]
    return _run(
        [
            str(HERE / "amygdala.py"),
            "--redis-url",
            f"{redis_url}/{os.environ.get('AMYGDALA_DB') or '11'}",
            "--vault",
            str(vault),
            "--brain-feed",
            str(vault / "brain-feed"),
        ]
    )


def scheduled(vault: Path, mode: str) -> int:
    if mode == "workstation" and not extract(vault).ok:
        print("FAILED: extraction phase")
        return 1
    stage, _ = pipeline(vault, [], "brain-cron")
    if stage == "maintenance":
        print("FAILED: maintenance phase")
        return 1
    ok = stage == "done"
    if not ok:
        print("FAILED: index phase")
    if mode == "workstation" and not amygdala(vault).ok:
        print("FAILED: amygdala check")
        ok = False
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("cycle", choices=("drain", "scheduled"))
    ap.add_argument("--vault", required=True, type=Path)
    ap.add_argument("--mode", default=os.environ.get("TICK_CYCLE_MODE", ""))
    args = ap.parse_args(argv)
    if args.cycle == "drain":
        return drain(args.vault)
    if args.mode not in MODES:
        print(f"ERROR: TICK_CYCLE_MODE must be one of {', '.join(MODES)}, got {args.mode!r}")
        return 2
    return scheduled(args.vault, args.mode)


if __name__ == "__main__":
    sys.exit(main())
