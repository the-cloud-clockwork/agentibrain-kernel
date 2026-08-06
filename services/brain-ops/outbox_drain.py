#!/usr/bin/env python3
"""Drain agentihooks marker buffers into brain-api over HTTP.

Replays every *.json marker file from the outbox (and its -backlog sibling,
where a stuck pile was parked in the SSH era) as POST {brain-api}/marker.
Files are deleted on 2xx, quarantined as .bad when unparseable, and left in
place on HTTP failure so the next pass retries them.

Idempotency-key parity with agentihooks brain_writer_hook: uuid5 of
"{session_id}-{type}-{content}", so a replay dedupes against the original
POST. The file's original `ts` rides along in attrs so brain-api backdates
the marker into its original dated vault files.

Safe to run concurrently with an agentihooks session draining the same dirs —
a file that vanishes mid-pass was simply won by the other drain.

Usage:
    python3 outbox_drain.py --outbox /agentihooks/brain-outbox \
        [--brain-url http://brain-api:8080] [--token $KB_ROUTER_TOKEN]

Env fallbacks: BRAIN_API_URL, KB_ROUTER_TOKEN.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path

REQ_TIMEOUT = 15
MAX_CONTENT_CHARS = 4096


def _marker_request(entry: dict) -> tuple[dict, str]:
    """Build the /marker POST body + idempotency key for one buffered file."""
    session_id = entry.get("session_id") or ""
    attrs = dict(entry.get("attrs") or {})
    attrs.setdefault("session_id", session_id)
    attrs.setdefault("source", entry.get("agent_name") or attrs.get("source") or "outbox-drain")
    if entry.get("project"):
        attrs.setdefault("project", entry["project"])
    if entry.get("ts"):
        attrs.setdefault("ts", entry["ts"])
    content = (entry.get("content") or "")[:MAX_CONTENT_CHARS]
    body = {"type": entry.get("type") or "", "content": content, "attrs": attrs}
    key_src = f"{session_id}-{body['type']}-{content}"
    idem = uuid.uuid5(uuid.NAMESPACE_URL, key_src).hex[:32]
    return body, idem


def _post_marker(brain_url: str, token: str, body: dict, idem: str) -> None:
    req = urllib.request.Request(
        url=f"{brain_url.rstrip('/')}/marker",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
            "X-Idempotency-Key": idem,
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=REQ_TIMEOUT):
        pass


def drain_dir(directory: Path, brain_url: str, token: str) -> dict[str, int]:
    stats = {"drained": 0, "quarantined": 0, "failed": 0}
    if not directory.is_dir():
        return stats
    for f in sorted(directory.glob("*.json")):
        try:
            entry = json.loads(f.read_text(encoding="utf-8"))
            body, idem = _marker_request(entry)
            if not body["type"] or not body["content"].strip():
                raise ValueError("missing type/content")
        except (json.JSONDecodeError, OSError, ValueError, TypeError) as exc:
            try:
                f.rename(f.with_suffix(".bad"))
                stats["quarantined"] += 1
                print(f"QUARANTINE: {f.name}: {exc}", file=sys.stderr)
            except OSError:
                pass
            continue

        try:
            _post_marker(brain_url, token, body, idem)
        except urllib.error.HTTPError as exc:
            if 400 <= exc.code < 500:
                # Server rejected the payload itself — retrying is futile.
                try:
                    f.rename(f.with_suffix(".bad"))
                    stats["quarantined"] += 1
                    print(f"QUARANTINE: {f.name}: HTTP {exc.code}", file=sys.stderr)
                except OSError:
                    pass
            else:
                stats["failed"] += 1
            continue
        except (urllib.error.URLError, OSError):
            stats["failed"] += 1
            continue

        try:
            f.unlink()
        except FileNotFoundError:
            pass  # concurrent drain won this file — already delivered
        stats["drained"] += 1
    return stats


def main() -> int:
    ap = argparse.ArgumentParser(description="Drain marker buffers into brain-api")
    ap.add_argument("--outbox", required=True, help="outbox dir; -backlog sibling is implied")
    ap.add_argument("--brain-url", default=os.environ.get("BRAIN_API_URL", "http://brain-api:8080"))
    ap.add_argument("--token", default=os.environ.get("KB_ROUTER_TOKEN", ""))
    args = ap.parse_args()

    if not args.token:
        print("ERROR: no token (set KB_ROUTER_TOKEN or --token)", file=sys.stderr)
        return 1

    outbox = Path(args.outbox)
    backlog = outbox.with_name(outbox.name + "-backlog")
    totals = {"drained": 0, "quarantined": 0, "failed": 0}
    for d in (outbox, backlog):
        st = drain_dir(d, args.brain_url, args.token)
        for k, v in st.items():
            totals[k] += v

    json.dump(totals, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0 if totals["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
