"""The vault this brain-api serves carries an owner claim naming one brain.

An unclaimed vault is claimed at startup; a vault claimed by another brain
refuses startup, so two deployments bound to one vault cannot both serve it.
The file matches agentibrain.backends.VAULT_CLAIM; this image does not ship
the agentibrain package.
"""

from __future__ import annotations

import json
from pathlib import Path

VAULT_CLAIM = ".brain-instance"
CLAIM_SCHEMA = "brain-claim-v1"


def claim_vault(root: Path, brain_id: str) -> None:
    claim = root / VAULT_CLAIM
    if not root.is_dir():
        return
    if claim.is_file():
        held = json.loads(claim.read_text()).get("brain_id")
        if held != brain_id:
            raise RuntimeError(f"vault {root} belongs to brain {held}, not {brain_id}")
        return
    tmp = root / f"{VAULT_CLAIM}.tmp"
    tmp.write_text(json.dumps({"schema": CLAIM_SCHEMA, "brain_id": brain_id}) + "\n")
    tmp.replace(claim)
