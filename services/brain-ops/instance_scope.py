"""The external state this brain owns: its vault and its Redis keys.

BRAIN_ID names the brain; unset is the default brain, which keeps the legacy
unprefixed Redis keys. A named brain prefixes every key it owns with
``brain:<id>:`` so two brains on one Redis never read each other's events.
The vault root and the brain's own event stream carry an owner claim, and a
claim held by another brain refuses the run before anything is read or written.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

DEFAULT_BRAIN_ID = "default"
VAULT_CLAIM = ".brain-instance"
CLAIM_SCHEMA = "brain-claim-v1"


class ScopeError(RuntimeError):
    pass


def brain_id() -> str:
    return os.getenv("BRAIN_ID", "").strip() or DEFAULT_BRAIN_ID


def namespaced(key: str) -> str:
    owner = brain_id()
    return key if owner == DEFAULT_BRAIN_ID else f"brain:{owner}:{key}"


def claim_vault(root: Path) -> None:
    owner = brain_id()
    claim = root / VAULT_CLAIM
    if claim.is_file():
        held = json.loads(claim.read_text()).get("brain_id")
        if held != owner:
            raise ScopeError(f"vault {root} belongs to brain {held}, not {owner}")
        return
    tmp = claim.with_name(f"{VAULT_CLAIM}.tmp")
    tmp.write_text(json.dumps({"schema": CLAIM_SCHEMA, "brain_id": owner}) + "\n")
    tmp.replace(claim)


def claim_stream(client, stream: str) -> None:
    owner = brain_id()
    key = f"{stream}:owner"
    client.set(key, owner, nx=True)
    held = client.get(key)
    if isinstance(held, bytes):
        held = held.decode()
    if held != owner:
        raise ScopeError(f"stream {stream} belongs to brain {held}, not {owner}")
