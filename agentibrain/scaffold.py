"""Vault scaffolder.

Owns the versioned vault layout. Writes ``.brain-schema`` as the single source
of truth for the layout version.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from agentibrain import __version__

SCHEMA_VERSION = "1"
SCHEMA_PRODUCER = f"agentibrain@{__version__}"

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

SCHEMA_FILENAME = ".brain-schema"


class SchemaConflict(RuntimeError):
    """Existing ``.brain-schema`` disagrees and ``force_upgrade`` is False."""


def _templates_root() -> Path:
    return Path(__file__).parent / "templates" / "vault-layout"


def _schema_payload() -> dict:
    return {
        "version": SCHEMA_VERSION,
        "schema": SCHEMA_PRODUCER,
        "created_at": datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
    }


def scaffold(vault_path: Path | str, *, force_upgrade: bool = False) -> dict:
    """Create the vault folder tree and write ``.brain-schema``.

    Returns a summary dict: ``{"vault": str, "folders_created": int, "schema": ...}``.
    """
    vault = Path(vault_path).expanduser().resolve()
    vault.mkdir(parents=True, exist_ok=True)

    schema_path = vault / SCHEMA_FILENAME
    new_payload = _schema_payload()
    if schema_path.exists():
        try:
            existing = json.loads(schema_path.read_text())
        except json.JSONDecodeError:
            existing = {}
        if existing.get("version") == SCHEMA_VERSION and not force_upgrade:
            # Same version — idempotent: keep existing schema timestamp.
            new_payload = existing
        elif existing.get("version") != SCHEMA_VERSION and not force_upgrade:
            raise SchemaConflict(
                f"{schema_path} reports version {existing.get('version')!r} but "
                f"scaffolder is {SCHEMA_VERSION!r}. Pass force_upgrade=True to overwrite."
            )

    created = 0
    for rel in VAULT_FOLDERS:
        folder = vault / rel
        if not folder.exists():
            folder.mkdir(parents=True, exist_ok=True)
            created += 1

    templates_src = _templates_root() / "templates"
    if templates_src.exists():
        dst = vault / "templates"
        for src_file in templates_src.glob("*.md"):
            target = dst / src_file.name
            if not target.exists():
                target.write_text(src_file.read_text())

    root_readme = _templates_root() / "README.md"
    dst_readme = vault / "README.md"
    if root_readme.exists() and not dst_readme.exists():
        dst_readme.write_text(root_readme.read_text())

    schema_path.write_text(json.dumps(new_payload, indent=2) + "\n")

    return {
        "vault": str(vault),
        "folders_created": created,
        "schema": new_payload,
    }
