# Tick Cadence — The Brain's Maintenance Loop (Priority 5)

The tick is the cognitive engine. It reconciles vault state, computes heat,
promotes/demotes arcs, runs AI reasoning, applies edges and signals, and
republishes `brain-feed/`. Without the tick, your `brain_ingest` writes and
`@marker` emissions never become visible to other sessions.

Entry point: `services/brain-ops/brain_tick.py::run_tick()`.

## When the Tick Fires (four paths)

| Path | Mechanism | Cadence |
|---|---|---|
| **Scheduled** | `brain-cron` CronJob — runs the full pipeline against NFS directly. | Every 2h at `HH:07 UTC` (`7 */2 * * *`). 04:00 UTC also runs `extract.py | cluster.py` first. |
| **On-demand HTTP** | `POST /tick` → `brain-api` writes a JSON request file to `brain-feed/ticks/requested/`. `tick-drain` CronJob drains it. | Drain runs every 2 min (`*/2 * * * *`). |
| **CLI** | `brain tick` subcommand calls the same `POST /tick` over HTTP. | Same as on-demand HTTP. |
| **brain-keeper** | The ops agent runs Phase 1 deterministically against NFS in-process for triage. | On dispatch only. |

`concurrencyPolicy: Forbid` on both CronJobs prevents overlap. The drain has
`activeDeadlineSeconds: 300`; the scheduled tick has `1200`.

## The Five Phases

### Phase 1 — Deterministic (`brain_keeper.tick()`)

Pure Python, target < 5s. No LLM. No network beyond NFS.

1. **Inbox drain** — `raw/inbox/*.md` → tag-to-region mapping → moved to region dirs (`left/`, `right/`, `bridge/`, `frontal-lobe/`, `pineal/`, `amygdala/`). Sets `region:` and `status:` frontmatter.
2. **Arc scan** — walks region dirs, then `clusters/<date>/` by date bucket. Deduplicates by stem; `.merged.md` suppresses its raw counterpart within the same directory only.
3. **Replay-edge boost** — counts arcs (< 14d) with `replayed_from:` frontmatter → builds boost map for heat.
4. **Heat recomputation** — arithmetic over `created`, `signals`, `source_sessions`, `status`. Decay after `BRAIN_DECAY_START_DAYS` (7). Writes `heat:` back to frontmatter.
5. **Promote / demote / graduate** — heat ≥ 5 copies to `frontal-lobe/conscious/`; heat < 3 moves from `conscious/` to `unconscious/`; heat ≤ 1 + age > 14d graduates to hemisphere.
6. **Workflow template extraction** — for hot active arcs (heat ≥ 4, status=active) with source sessions, extracts step templates from transcripts and appends `## Workflow Template` section.
7. **Mitigation map** — resolved/graduated arcs with `mitigates:` tombstone matching signals.
8. **Brain-feed writes** — `hot-arcs.md` (top 10), `signals.md`, `inject.md`, `_dashboard.md` per date dir.

Reads: NFS `/vault/**/*.md`. Writes: `brain-feed/*.md` + arc frontmatter heat fields.

### Phase 2 — Prompt Generation (`brain_tick_prompt.build_prompt()`)

Re-runs `brain_keeper.tick()` to get deterministic stats, then builds the AI
prompt: pre-computed arc table (cluster_id, heat, region, status, title),
signals, lessons, inject blocks, existing edge map, delta since last tick,
deduped lessons. Logs prompt length.

### Phase 3 — AI Reasoning (`call_llm()`)

```
POST {INFERENCE_URL}/v1/chat/completions
  model = BRAIN_BRIEF_MODEL  (default: "brain-brief", a LiteLLM alias)
  max_tokens = 4096
  temperature = 0.3
```

Returns a structured markdown response with five numbered sections (edges,
merges, signal changes, intent, health). **Skipped entirely** when
`INFERENCE_URL` is empty or `--no-ai` is passed to `brain_tick.py`.

### Phase 4 — Apply (`brain_apply.apply()`)

Parses the AI output and applies each section deterministically:

1. **Edges** — `parse_edges` + `apply_edges` insert `<!-- @edge type=X target=Y -->` into source arc files. Two-pass collapse: same-pair multi-type collapsed by priority (`parent > child > sibling > unblocks > supersedes > related`). Cross-tick `any_to_target` guard blocks accumulation.
2. **Merges** — append arc B content to arc A; rename B to `.merged.md`.
3. **Signal changes** — update or clear `<!-- @signal severity=X source=Y -->` markers in arc files. Fuzzy-match fallback for legacy bare markers.
4. **Operator intent** — write `brain-feed/intent.md` with `priority: 7  ttl: 1800`.
5. **Health** — append `{ts, score, reason, arcs, signals, lessons}` to `brain-etl/health.jsonl`.

Saves raw AI output to `brain-etl/ticks/<ts>-ai-output.md` for audit. Writes
`brain-feed/last-tick-diff.md`.

### Phase 5 — Verify

Runs `brain_keeper.tick(dry_run=True)` to confirm changes persisted. Stats
logged, no writes.

## Post-Tick (always)

After Phase 5, regardless of how the tick was triggered:

- **ClickHouse** — `INSERT INTO brain.tick_health` row: `{ts, score, reason, arcs_scanned, signals_collected, heat_changes, promotions, demotions, graduations, total_ms, tick_type, signal_tombstone_counts}`. MergeTree, 90d TTL. Best-effort; skipped when `CLICKHOUSE_URL` is empty.
- **Redis event bus** — `XADD anton:events:brain` (Redis DB 11, `maxlen=10000`):
  ```
  {v, domain, event="brain.tick.complete", source="brain-cron",
   severity, host, ts, title, message, score, arcs_scanned, ...}
  ```
  Severity is set **explicitly** by `_classify_tick_severity()`:
  - score = 0 or AI failure → `nuclear`
  - score 1-3 → `warning`
  - score 4+ → `info`
- **Embeddings refresh** — `embed_arcs.py --prune` walks all arc files, embeds new/changed content via `POST embeddings/embed`, then prunes orphans by producer.
- **Amygdala one-shot** — consumer reads stream once, writes/clears `amygdala-active.md`.

## How to Trigger a Tick

You have no MCP tool for `POST /tick` — that's operator-only via:
- `curl -H "Authorization: Bearer $KB_ROUTER_TOKEN" -X POST $BRAIN_URL/tick` (from operator session, not yours)
- `brain tick` CLI (operator)

What you CAN do: ingest, write markers, and trust the cadence. If the
operator wants your write visible immediately, they trigger the tick.

## How to See Tick Results

You receive tick output passively as `BROADCAST [INFO]` blocks at:

- **Next SessionStart** — `brain_adapter` reads `/feed`, injects `last-tick-diff`, `Active Hot Arcs`, `Operator Intent`, signals.
- **Every 30 turns** — `brain_adapter.maybe_refresh()` republishes if content hash changed.
- **`brain_refresh` MCP tool** — force a re-read mid-session (rare).

The `last-tick-diff` block is the canonical "what changed this tick" summary.
Read it on SessionStart.

## When the Tick Skips Phases

| Condition | Effect |
|---|---|
| `INFERENCE_URL` empty | Phases 3-4 skipped. Phase 1 (deterministic) still runs — heat, promote/demote, brain-feed writes happen. |
| `--no-ai` flag | Same as empty `INFERENCE_URL`. |
| `--dry-run` flag | All phases run, no writes. Used by Phase 5 verify. |
| `CLICKHOUSE_URL` empty | Post-tick metrics insert skipped. |
| Phase 1 fails | Whole tick aborts. Redis event still emitted with `severity=nuclear`. |
| AI returns malformed sections | Phase 4 applies the well-formed sections, logs the rest. Score reflects partial success. |

## Cost Profile

| Phase | Network | LLM tokens | Wall-clock (typical) |
|---|---|---|---|
| 1 (deterministic) | NFS only | 0 | < 5s |
| 2 (prompt build) | NFS read | 0 | < 1s |
| 3 (AI reasoning) | LiteLLM | ~prompt + 4096 out | 10-30s |
| 4 (apply) | NFS write | 0 | < 2s |
| 5 (verify) | NFS read | 0 | < 5s |
| Post: embeddings | LiteLLM embed per changed arc | ~chunks × 1 embed | 5-20s |
| Post: ClickHouse + Redis | HTTP + Redis | 0 | < 1s |

Full tick: 30-60s typical, hard deadline 1200s.
