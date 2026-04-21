"""Vault scaffolder.

Owns the versioned vault layout. Writes ``.brain-schema`` as the single source
of truth for the layout version. Everything else is sourced from the packaged
``templates/vault-layout/`` tree — directories, README files, note templates,
and MUBS templates. Adding a new folder or file to the template ships it to
every future scaffold without touching this module.
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

from agentibrain import __version__

SCHEMA_VERSION = "1"
SCHEMA_PRODUCER = f"agentibrain@{__version__}"
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


def _copy_tree(src: Path, dst: Path) -> tuple[int, int]:
    """Copy ``src`` into ``dst`` recursively.

    Returns ``(folders_created, files_written)``. Never overwrites an existing
    file — the operator's edits always win over the shipped template.
    """
    folders_created = 0
    files_written = 0
    for entry in src.rglob("*"):
        rel = entry.relative_to(src)
        target = dst / rel
        if entry.is_dir():
            if not target.exists():
                target.mkdir(parents=True, exist_ok=True)
                folders_created += 1
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                shutil.copyfile(entry, target)
                files_written += 1
    return folders_created, files_written


def scaffold(vault_path: Path | str, *, force_upgrade: bool = False) -> dict:
    """Create (or top up) the vault tree and write ``.brain-schema``.

    Returns a summary dict::

        {
            "vault": <absolute path>,
            "folders_created": <int>,
            "files_written": <int>,
            "schema": {"version", "schema", "created_at"},
        }

    The call is idempotent: existing files and folders are never overwritten.
    Only missing pieces are filled in. When an existing ``.brain-schema`` has
    a different version, ``SchemaConflict`` is raised unless ``force_upgrade``
    is True.
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

    folders_created, files_written = _copy_tree(_templates_root(), vault)
    schema_path.write_text(json.dumps(new_payload, indent=2) + "\n")

    return {
        "vault": str(vault),
        "folders_created": folders_created,
        "files_written": files_written,
        "schema": new_payload,
    }
