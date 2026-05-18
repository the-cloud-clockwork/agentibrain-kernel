# Vault Layout — Schema v1 (Priority 4)

The vault is the brain's persistent store. It lives on NFS at `/vault` (mounted
into brain-api and brain-ops). You never touch it directly — writes go through
`brain_ingest` MCP or `@marker` HTML comments; reads go through `kb_search`,
`brain_search_arcs`, or `GET /vault/*` via brain-api.

Schema version is tracked in `.brain-schema` at the vault root. The only
authoritative writer of the layout is `brain scaffold` (CLI subcommand of
`agentibrain`).

## Top-Level Tree

```
<vault>/
  .brain-schema                    # JSON: version, producer, created_at
  README.md
  CLAUDE.md                        # vault rules for agents (separate from this file)

  raw/                             # OWNER: brain-api /ingest
    inbox/                         # /ingest drops land here, pre-classification
    articles/  transcripts/  media/

  clusters/                        # OWNER: tick engine
    <YYYY-MM-DD>/                  # date-bucketed arcs from extract.py|cluster.py
      <cluster_id>.md              # frontmatter: cluster_id, heat, region, status, created, signals, source_sessions
      <cluster_id>.merged.md       # tombstone — content was merged into another arc
      _dashboard.md                # heat-sorted table, written by brain_keeper per tick

  brain-feed/                      # OWNER: tick engine (brain_keeper writes)
    hot-arcs.md        (priority: 10, ttl: 3600)  top-10 arcs by heat
    signals.md         (priority:  8, ttl: 3600)  active @signal markers
    inject.md          (priority:  9, ttl: 3600)  active @inject blocks
    intent.md          (priority:  7, ttl: 1800)  operator intent from AI tick
    amygdala-active.md (priority:100, ttl:  300)  alert active flag (file existence = signal)
    last-tick-diff.md  (priority:  5, ttl: 7200)  diff summary from last tick
    ticks/
      requested/                   # on-demand tick JSON files (brain-api /tick)
      completed/                   # drained + succeeded
      failed/                      # drained + non-zero exit

  amygdala/                        # OWNER: amygdala daemon
    <ts>-<slug>.md                 # incident arcs for nuclear/critical events (heat:10, status:active)

  frontal-lobe/                    # OWNER: brain_keeper promote/demote
    conscious/                     # heat >= 5 (auto-promoted copies)
    unconscious/                   # heat <  3 (demoted from conscious)
  pineal/                          # joy / breakthrough region

  brain-etl/                       # OWNER: brain_apply
    health.jsonl                   # append-only health-score time series
    ticks/
      <ts>-ai-output.md            # raw AI reasoning output per tick (audit)

  # Knowledge regions (operator + agents)
  identity/                        # who the operator is
  left/                            # technical hemisphere
    projects/  research/  reference/  decisions/  incidents/
  right/                           # creative hemisphere
    ideas/  strategy/  life/  creative/  risk/
  bridge/                          # cross-hemisphere synthesis
    vision.md  connections.md  weekly-synthesis.md
  daily/                           # append-only daily logs
  templates/                       # note starters + MUBS folder template
```

## What You Touch vs What the Tick Touches

| Path | Written by | Read by |
|---|---|---|
| `raw/inbox/` | `brain_ingest` MCP / brain-api `/ingest` | tick Phase 1 (drained → moved to region) |
| `brain-feed/*.md` | tick engine, amygdala daemon | brain-api `/feed`, your context via hooks |
| `clusters/<date>/*.md` | `extract.py | cluster.py` (04:00 UTC daily) | `brain_search_arcs`, `kb_search` |
| `amygdala/*.md` | amygdala daemon (Redis stream consumer) | tick, `kb_search` |
| `frontal-lobe/*` | tick (promote/demote logic) | `brain_search_arcs` (heat filter) |
| `left/ right/ bridge/ pineal/` | tick (classify from inbox) | `kb_search`, `brain_search_arcs` |
| `brain-etl/` | tick Phase 4 (`brain_apply`) | ops/health audit only |
| `daily/`, `identity/`, `templates/` | operator (rare; manual) | `kb_search` |

You write to nothing directly. Everything goes via `brain_ingest` →
`raw/inbox/` → tick classifies you into the right region.

## Frontmatter Conventions

Files in `brain-feed/` have YAML frontmatter that hooks parse:

```yaml
---
id: hot-arcs
title: Active Hot Arcs
priority: 10        # broadcast injection ordering, higher = sooner
ttl: 3600           # seconds; expired entries are dropped
severity: info      # info | alert | critical | nuclear
---
```

Arc files in `clusters/` and region dirs have richer frontmatter:

```yaml
---
cluster_id: 2026-05-18-brain-tick-fix
title: Brain Tick Fix
heat: 7             # int; promote >= 5, demote < 3, graduate <= 1
region: bridge      # left | right | bridge | frontal-lobe | pineal | amygdala
status: active      # active | resolved | graduated | merged
created: 2026-05-18
signals: 2
source_sessions: [session_abc, session_def]
replayed_from: 2026-04-12-original-incident   # optional, drives replay-edge boost
mitigates: 2026-05-01-original-signal         # optional, tombstones the signal
---
```

Without frontmatter, `/feed` skips the file. Without `priority`, broadcast
ordering falls to default and the file may be deprioritized.

## Inline Markers (HTML comments)

Arc files embed structured markers as HTML comments. The tick parses these on
every scan.

```markdown
<!-- @signal severity=warning source=deploy -->
publisher-0 restarted 3 times in 10 minutes
<!-- @/signal -->

<!-- @edge type=related target=2026-04-12-restart-loop -->

<!-- @inject target=all -->
When debugging publisher restarts, check Redis backpressure first.
<!-- @/inject -->

<!-- @lesson -->
psycopg2 connections are not thread-safe — use ThreadedConnectionPool.
<!-- @/lesson -->
```

| Marker | What the tick does |
|---|---|
| `@signal` | Collected into `brain-feed/signals.md`. Severity `nuclear`/`critical` triggers amygdala write. |
| `@edge` | Becomes a graph edge between arcs. Collapsed by `(source, target)` with type priority (parent > child > sibling > unblocks > supersedes > related). Cross-tick guard blocks accumulation. |
| `@inject` | Collected into `brain-feed/inject.md`, deduped. Injected into agent context as `BROADCAST [INFO]` block on next session. |
| `@lesson` | Captured by `brain_writer_hook` from your output → `/marker` → folded into arc. |
| `@milestone` | Same path as `@lesson`. Marks deliverable completion. |
| `@decision` | Same path. Architectural choice with date. |

Full marker syntax and emission rules: see `02-brain-markers.md`.

## Heat Mechanics

- `compute_heat` is pure arithmetic over: `created` (recency), `signals` (count), `source_sessions` (count), `status` (active=full, resolved=halved), and replay-edge boost (recent `replayed_from` references).
- Decay starts at `BRAIN_DECAY_START_DAYS` (default 7) and continues at `BRAIN_DECAY_INTERVAL_DAYS` cadence.
- Promote threshold: heat ≥ `BRAIN_PROMOTE_HEAT` (default 5) → copy to `frontal-lobe/conscious/`.
- Demote threshold: heat < `BRAIN_DEMOTE_HEAT` (default 3) → move from `conscious/` to `unconscious/`.
- Graduate threshold: heat ≤ `BRAIN_GRADUATE_HEAT` (default 1) AND age > 14 days → move to hemisphere region (`left/`, `right/`, etc.).

Heat is written back to arc frontmatter in-place on every tick. Stale arcs
cool off; replayed-from arcs warm back up.

## Schema Version & Conflict Handling

The `.brain-schema` file at vault root pins the current schema version:

```json
{"version": 1, "producer": "brain scaffold v0.6.1", "created_at": "..."}
```

If `brain scaffold` is run against a vault with a different schema version,
it raises `SchemaConflict` unless `--force-upgrade` is passed. Do not edit
`.brain-schema` by hand.
