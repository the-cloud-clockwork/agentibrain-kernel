# Vault Schema

The vault is the on-disk home of the brain: cluster markdown, hot-arc feed, operator intent, pipeline metadata, emergency signals, daily logs, and the operator's own dual-hemisphere knowledge base. It is an Obsidian-compatible folder tree, writable by humans and by kernel services.

`local/bootstrap.sh` scaffolds the vault tree on first compose run; the schema marker is `.brain-schema` (JSON). The tree itself can be edited freely — bootstrap is idempotent and never overwrites existing files.

## Layout (v1)

```
<vault>/
  .brain-schema           # version marker (JSON, owned by scaffold)
  README.md               # quick tour + first steps
  CLAUDE.md               # vault rules for AI agents

  # Cognitive regions (owned by the tick-engine + agents)
  raw/                    # ingest staging — never edit directly
    inbox/                # /ingest raw drops, pre-classification
    articles/             # fetched articles/papers
    media/                # images, audio, video
    transcripts/          # session transcripts
  clusters/               # canonical arc storage (owner: tick-engine)
  brain-feed/             # hot arc feed (owner: tick-engine)
    ticks/                # per-tick run records
  amygdala/               # emergency signals (owner: amygdala daemon)
  frontal-lobe/           # working memory
    conscious/            # heat ≥ 7 — auto-injected into agent context
    unconscious/          # cooled arcs, still linked
  pineal/                 # joy & breakthrough region

  # Knowledge base (owned by the operator + curated by agents)
  identity/               # who the operator is — root node
    README.md
    about-me.template.md  # → rename to about-me.md and fill in
    goals.template.md
    principles.template.md
    stack.template.md
  left/                   # technical + systematic hemisphere
    _index.md
    projects/             # MUBS instances per project
    research/             # spikes + prior-art reviews
    reference/            # stable how-tos + patterns
    decisions/            # ADRs
    incidents/            # resolved post-mortems
  right/                  # creative + strategic hemisphere
    _index.md
    ideas/                # idea MUBS instances
    strategy/             # long-horizon strategic notes
    life/                 # life design (activate when ready)
    creative/             # non-work projects (activate when ready)
    risk/                 # risk frameworks, bets
  bridge/                 # cross-hemisphere synthesis
    _index.md
    vision.md             # strategic brief for the vault
    connections.md        # discovered left↔right links
    weekly-synthesis.md   # rolling weekly reflection
  daily/                  # append-only daily logs

  # Templates
  templates/              # starting points for new notes
    README.md
    decision.md           # ADR starter
    idea.md               # idea capture starter
    incident.md           # incident post-mortem starter
    project.md            # project pointer starter
    research.md           # research spike starter
    mubs/                 # Minimal Unit of Brain Storage — whole-folder template
      VISION.md
      SPECS.md
      BLOCKS.md
      TODO.md
      STATE.md
      BUGS.md
      KNOWN-ISSUES.md
      ENHANCEMENTS.md
      MVP.md
      PATCHES.md
```

The tree is exactly what `local/bootstrap.sh` seeds — it is shipped inside the `agentibrain` wheel under `agentibrain/templates/vault-layout/`.

## `.brain-schema`

```json
{
  "version": "1",
  "schema": "agentibrain@0.1.0",
  "created_at": "2026-04-21T15:45:52+00:00"
}
```

- `version` — schema version the vault was scaffolded with. Must match the kernel's `SCHEMA_VERSION`.
- `schema` — producer identifier (`agentibrain@<package-version>`).
- `created_at` — RFC 3339 timestamp of initial scaffold (preserved across idempotent re-runs).

## Rules

1. **Scaffold is idempotent** — re-running `local/bootstrap.sh` against an existing vault at the same version is a no-op (`folders_created=0`, `files_written=0`). Operator edits are never clobbered.
2. **Version mismatch is an error** — if `.brain-schema` reports a different version, scaffold raises `SchemaConflict` unless `--force-upgrade` is passed.
3. **Readers never mutate the schema** — hooks, agents, and CLI tools should treat `.brain-schema` as read-only and refuse to operate on an unfamiliar version.
4. **Folder ownership** is a soft convention; enforcement is by producer name, not filesystem permissions. Keep writers scoped to their domain:
   - `tick-engine` writes `clusters/`, `brain-feed/`, `pineal/`
   - `amygdala daemon` writes `amygdala/`
   - `/ingest` (obsidian-reader) writes `raw/inbox/`
   - Humans own `identity/`, `left/` (except `left/incidents/` which is curated from `amygdala/`), `right/`, `bridge/`, `daily/`
   - `brain-keeper daemon` promotes/demotes arcs between `clusters/` and `frontal-lobe/`

## Obsidian compatibility

The layout is Obsidian-compatible out of the box — wikilinks, backlinks, graph view, and frontmatter queries all work. The operator opens the vault folder in the Obsidian desktop app as a visual layer on top of the same markdown tree the kernel services write. Obsidian is **not a runtime dependency** of the kernel; it is an optional human UI.

## Upgrading

A v1 → v2 schema migration tool will ship in a future kernel release. Until then, rely on `scaffold(..., force_upgrade=True)` and a short manual migration note under `pineal/`.
