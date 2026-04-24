"""Signal reader — current amygdala emergency status.

Reads `$VAULT_ROOT/brain-feed/amygdala-active.md` — the single-file contract
that `tick-engine/amygdala.py` maintains. File absent => no active signal.
This is the HTTP replacement for the filesystem stat used by
`agentihooks/hooks/context/amygdala_hook.py::check_amygdala`.
"""

from __future__ import annotations

import hashlib
import os
from datetime import datetime, timezone
from pathlib import Path

from .feed import _parse_frontmatter


VAULT_ROOT = Path(os.environ.get("VAULT_ROOT", "/vault")).resolve()
AMYGDALA_SIGNAL_PATH = os.environ.get(
    "AMYGDALA_SIGNAL_PATH", "brain-feed/amygdala-active.md"
).lstrip("/")

_VALID_SEVERITIES = {"nuclear", "critical", "warning", "info", "resolved"}


def read_signal(vault_root: Path | None = None) -> dict:
    """Return current amygdala signal state.

    Response shape:
        {
          "active": bool,
          "severity": "nuclear"|"critical"|"warning"|"info"|"resolved"|null,
          "title": str|null,
          "content": str|null,
          "hash": str|null,                 # content sha for dedup
          "last_updated": ISO8601|null,     # file mtime
          "vault_path": str,
        }
    """
    root = Path(vault_root) if vault_root else VAULT_ROOT
    path = root / AMYGDALA_SIGNAL_PATH

    base = {
        "active": False,
        "severity": None,
        "title": None,
        "content": None,
        "hash": None,
        "last_updated": None,
        "vault_path": str(AMYGDALA_SIGNAL_PATH),
    }
    if not path.exists() or not path.is_file():
        return base

    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return base

    fm, body = _parse_frontmatter(text)
    severity = (fm.get("severity") or "critical").lower()
    if severity not in _VALID_SEVERITIES:
        severity = "critical"

    content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    try:
        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat(
            timespec="seconds"
        )
    except OSError:
        mtime = None

    return {
        "active": severity != "resolved",
        "severity": severity,
        "title": fm.get("title") or "AMYGDALA ALERT",
        "content": body.strip() or None,
        "hash": content_hash,
        "last_updated": mtime,
        "vault_path": str(AMYGDALA_SIGNAL_PATH),
    }
