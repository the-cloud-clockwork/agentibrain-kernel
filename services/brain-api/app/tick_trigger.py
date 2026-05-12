"""Tick trigger — file-based request protocol for manual brain ticks.

POST /tick writes a request file to `$VAULT_ROOT/brain-feed/ticks/requested/`
and returns 202. The tick-engine CronJob (or brain-keeper agent) picks up
the request, runs `brain_tick.py::run_tick`, and moves the file to either
`ticks/completed/` or `ticks/failed/`. Clients poll GET /tick/{job_id} for
status.

Rationale: brain-api and brain-ops live in separate pods with separate
images. Calling `run_tick()` inline would require bundling the entire
brain-ops code + deps into brain-api, which defeats the split. The file
protocol keeps responsibilities clean and avoids new RPCs.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4


VAULT_ROOT = Path(os.environ.get("VAULT_ROOT", "/vault")).resolve()
TICK_REQUESTS_DIR = os.environ.get(
    "TICK_REQUESTS_DIR", "brain-feed/ticks/requested"
).strip("/")
TICK_COMPLETED_DIR = os.environ.get(
    "TICK_COMPLETED_DIR", "brain-feed/ticks/completed"
).strip("/")
TICK_FAILED_DIR = os.environ.get(
    "TICK_FAILED_DIR", "brain-feed/ticks/failed"
).strip("/")


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds")


def enqueue_tick(
    dry_run: bool = False,
    no_ai: bool = False,
    source: str = "brain-api",
    vault_root: Path | None = None,
) -> dict:
    """Write a tick request file. Returns {job_id, requested_at, request_path}."""
    root = Path(vault_root) if vault_root else VAULT_ROOT
    req_dir = root / TICK_REQUESTS_DIR
    req_dir.mkdir(parents=True, exist_ok=True)

    job_id = uuid4().hex[:12]
    requested_at = _now_iso()
    payload: dict[str, Any] = {
        "job_id": job_id,
        "requested_at": requested_at,
        "source": source,
        "dry_run": bool(dry_run),
        "no_ai": bool(no_ai),
        "status": "pending",
    }

    fname = requested_at.replace(":", "-") + f"-{job_id}.json"
    target = req_dir / fname
    target.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")

    return {
        "job_id": job_id,
        "requested_at": requested_at,
        "request_path": str(target.relative_to(root)),
        "dry_run": bool(dry_run),
        "no_ai": bool(no_ai),
        "status": "pending",
    }


def get_tick_status(job_id: str, vault_root: Path | None = None) -> dict:
    """Look up a tick job by scanning requested/, completed/, and failed/ dirs."""
    root = Path(vault_root) if vault_root else VAULT_ROOT
    for state, rel_dir in (
        ("completed", TICK_COMPLETED_DIR),
        ("failed", TICK_FAILED_DIR),
        ("pending", TICK_REQUESTS_DIR),
    ):
        dir_path = root / rel_dir
        if not dir_path.is_dir():
            continue
        for child in dir_path.iterdir():
            if not child.is_file() or job_id not in child.name:
                continue
            try:
                data = json.loads(child.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            data["status"] = state
            data["job_id"] = job_id
            data["path"] = str(child.relative_to(root))
            return data
    return {"job_id": job_id, "status": "unknown"}
