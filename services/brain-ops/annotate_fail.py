#!/usr/bin/env python3
"""Attach a failing tick's log tail to its failed request file.

tick-drain moves a request to ticks/failed/ when brain_tick exits non-zero,
but the file itself carried no error — GET /tick/{job_id} (and therefore
`agentibrain sync --check`) could only say "failed" and leave the operator
digging through container logs. This stamps the last 2KB of the tick's
combined output into the request as `error_tail` so the status endpoint
answers "why" directly.

Usage: annotate_fail.py <failed-request.json> <tick-log-file>
Never exits non-zero — annotation is best-effort and must not disturb the
drain loop's own accounting.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

TAIL_CHARS = 2000


def main() -> int:
    if len(sys.argv) != 3:
        return 0
    req, log = Path(sys.argv[1]), Path(sys.argv[2])
    try:
        data = json.loads(req.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return 0
    try:
        tail = log.read_text(encoding="utf-8", errors="replace")[-TAIL_CHARS:]
    except OSError:
        return 0
    if not tail.strip():
        return 0
    data["error_tail"] = tail
    try:
        req.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
