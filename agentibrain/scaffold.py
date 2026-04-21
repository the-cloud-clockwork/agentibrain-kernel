"""Vault scaffolder — implemented in Phase 5.

Owns the versioned vault layout. Writes ``.brain-schema`` as the single source
of truth for the layout version.
"""

from __future__ import annotations

from pathlib import Path

SCHEMA_VERSION = "1"
SCHEMA_PRODUCER = "agentibrain@0.1.0"

VAULT_FOLDERS: tuple[str, ...] = (
    "raw/inbox",
    "clusters",
    "brain-feed",
    "amygdala",
    "frontal-lobe",
    "pineal",
    "_index",
    "templates",
)


def scaffold(vault_path: Path, *, force_upgrade: bool = False) -> None:
    """Create the vault folder tree and write ``.brain-schema``. Phase 5."""
    raise NotImplementedError("scaffold.scaffold lands in Phase 5")
