# Vault — agentibrain

This vault is seeded by `brain scaffold`. The layout is a versioned schema
owned by the kernel.

## Folders

- `raw/inbox/` — pre-classification drops from `/ingest`.
- `clusters/` — arc clusters written by the tick-engine.
- `brain-feed/` — hot arcs synced to the fleet (read-only for consumers).
- `amygdala/` — emergency signals.
- `frontal-lobe/` — human-owned notes (operator intent, manual context).
- `pineal/` — pipeline metadata (tick logs, heat scores).
- `_index/` — auto-generated index files.
- `templates/` — arc + cluster templates.

Schema version is recorded in `.brain-schema`. Do not edit by hand.
