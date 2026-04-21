# Vault Schema

The vault is the on-disk home of the brain: cluster markdown, hot-arc feed,
operator intent, pipeline metadata, emergency signals. It is an
Obsidian-compatible folder tree, writable by humans and by kernel services.

`brain scaffold <vault-path>` is the only authoritative writer of the schema.

## Layout (v1)

```
<vault>/
  .brain-schema           # version marker (JSON, owned by scaffold)
  README.md               # seeded from templates/vault-layout/
  raw/
    inbox/                # /ingest raw drops, pre-classification
  clusters/               # arc clusters (owner: tick-engine)
  brain-feed/             # hot arcs, synced to fleet (owner: brain_apply.py)
  amygdala/               # emergency signals (owner: amygdala.py)
  frontal-lobe/           # operator intent, manual notes (human-owned)
  pineal/                 # pipeline metadata (tick logs, heat scores)
  _index/                 # auto-generated index files
  templates/              # arc + cluster templates (seed copy)
```

## `.brain-schema`

```json
{
  "version": "1",
  "schema": "agentibrain@0.1.0",
  "created_at": "2026-04-21T15:45:52+00:00"
}
```

- `version` — schema version the vault was scaffolded with. Must match the
  kernel's `SCHEMA_VERSION`.
- `schema` — producer identifier (`agentibrain@<package-version>`).
- `created_at` — RFC 3339 timestamp of initial scaffold (preserved across
  idempotent re-runs).

## Rules

1. **Scaffold is idempotent** — re-running `brain scaffold` against an existing
   vault at the same version is a no-op (folders_created=0).
2. **Version mismatch is an error** — if `.brain-schema` reports a different
   version, scaffold raises `SchemaConflict` unless `--force-upgrade` is passed.
3. **Readers never mutate the schema** — hooks, agents, and CLI tools should
   treat `.brain-schema` as read-only and refuse to operate on an unfamiliar
   version.
4. **Folder ownership** is a soft convention; enforcement is by producer name,
   not filesystem permissions. Keep writers scoped to their domain:
   - `tick-engine` writes `clusters/`, `pineal/`, `_index/`
   - `brain_apply.py` writes `brain-feed/`
   - `amygdala.py` writes `amygdala/`
   - `/ingest` via obsidian-reader writes `raw/inbox/`
   - Humans own `frontal-lobe/`

## Upgrading

A v1 → v2 schema migration tool will ship in a future kernel release. Until
then, rely on `scaffold(..., force_upgrade=True)` and a short manual migration
note under `pineal/`.
