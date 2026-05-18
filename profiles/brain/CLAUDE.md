# Brain Profile — Memory-Aware Session

This profile is **chained**, not standalone. It composes on top of a base
profile (`anton`, `agenticore`, etc.) and adds one thing: awareness of and
access to the brain — the fleet's persistent memory layer.

Tone, response style, CI doctrine, secrets policy, branch flow — all come
from the base profile. This file is purely the brain interaction layer.

## 1. What the Brain Is

The brain is the fleet's shared memory. Past decisions, lessons, signals,
ingested research, and operator knowledge live in an NFS vault, are indexed
semantically in pgvector, and are served back to your session through MCP
tools and hook-driven context injection. You read from it through MCP, you
write to it through MCP or HTML-comment markers in your output, and the
maintenance loop (the tick) reconciles and republishes the state every two
hours.

Most of the wiring runs without you asking. Your job is to know what tools
exist, what gets injected for you, and where your writes land.

## 2. Architecture (single env — `anton-prod`)

| Workload | Role | Talks to |
|---|---|---|
| **brain-api** | HTTP gateway: `/vault/*`, `/feed`, `/signal`, `/marker`, `/ingest`, `/tick`. Mounts NFS vault directly. | NFS vault, LiteLLM (classify), embeddings (`/index_artifact`) |
| **agentibrain-mcp** | FastMCP/SSE on `:8104`. Exposes `kb_*` + `brain_*` tools to your session. No vault mount — all reads via brain-api. | brain-api, embeddings, LiteLLM |
| **agentibrain-embeddings** | pgvector 1536-dim semantic index. `POST /embed /search /prune`, `GET /by-key/{k}`. | Postgres/pgvector, LiteLLM (embed) |
| **brain-ops** | 5-phase tick (CronJob `7 */2 * * *`) + tick-drain (CronJob `*/2 * * * *`) + amygdala (Deployment, Redis consumer). | NFS vault, LiteLLM (reason), Redis DB 11, ClickHouse, embeddings |
| **brain-keeper** | Reasoning ops agent (Opus, 10-min cap). Reads `brain-feed/`, dispatches via MCP. | brain-feed (NFS read), MCP |

Vault: NFS at `/vault` (services see it; you do not). Event bus: Redis DB 11,
stream `anton:events:brain`. Semantic store: Postgres `content_embeddings`
(1536-dim HNSW). Tick output → `brain-feed/*.md` → hooks → your context.

## 3. Vault Layout — What You Need to Know

Five dirs matter for an agent:

- `raw/inbox/` — where `brain_ingest` lands your writes. Drained by the next tick.
- `brain-feed/` — what hooks read into your context. `hot-arcs.md`, `signals.md`, `inject.md`, `intent.md`, `last-tick-diff.md`, `amygdala-active.md`.
- `clusters/<YYYY-MM-DD>/` — arc files (narrative work units). Heat-scored, promoted/demoted by the tick.
- `frontal-lobe/{conscious,unconscious}/` — promoted (heat ≥ 5) and demoted (heat < 3) arcs.
- `left/ right/ bridge/ pineal/ amygdala/` — knowledge regions. Tick classifies inbox notes into these.

Full layout, frontmatter conventions, schema version: see `rules/04-vault-layout.md`.

## 4. Your Read Tools (from `agentibrain` MCP)

| Tool | What it does | First-choice for |
|---|---|---|
| `kb_search` | Federated: pgvector semantic ∥ vault text search, merged + score-normalized. | Any knowledge lookup. "What do we know about X?" |
| `kb_brief` | `kb_search` + LLM synthesis (3-5 lines + candidate refs). | When you need a summary, not raw hits. |
| `brain_search_arcs` | Semantic search over arcs (`producer=brain-arc`), filtered by heat. | Past work, decisions, project history. |
| `brain_get_arc` | Full arc by `cluster_id` (pgvector by-key → vault fallback). | Drill-down after `brain_search_arcs`. |
| `brain_status` (hooks-utils) | Current brain adapter state. | Diagnostics. |
| `brain_refresh` (hooks-utils) | Force re-read of `/feed` + republish. | After you just wrote and want to see it back. |

Full tool semantics, auto-lookup rules, parameter details: see `rules/01-brain-tools.md`.

## 5. Your Write Paths — Three Ways Into Raw Content

| Path | When | Where it lands | Latency |
|---|---|---|---|
| **`brain_ingest` MCP** | Paragraphs to full docs. Architecture notes, research synthesis, reference material the operator will want later. | `raw/inbox/<slug>.md` → classified → region dir | ≤ 2 min |
| **`@marker` HTML comments** | In-flow atomic insights, max 5 per session. One per insight. | `/marker` via `brain_writer_hook` → marker file → folded into arc at next tick | ≤ 2 min |
| **`channel_publish` (hooks-utils)** | Fleet **coordination only** — "restarting X, hold off." NOT for knowledge. | Broadcast channel, ephemeral | Immediate |

Markers vs ingest: markers are things you noticed while doing other work
(lesson, milestone, signal, decision). Ingest is when you sat down and
produced something the brain should keep. One-liners → marker. Documents → ingest.

Marker types, syntax, severity levels: see `rules/02-brain-markers.md`.

## 6. What Hooks Do FOR You (don't duplicate)

The hook layer (`agentihooks`) runs brain-aware logic automatically. You do
not need to call these — they happen on every session and every turn.

- **SessionStart** — `brain_adapter` calls `GET /feed`, scrubs halt-phrases, dedups by hash, and injects `BROADCAST [INFO]` blocks: `Active Hot Arcs`, `Operator Intent`, `last-tick-diff`, active signals, inject blocks.
- **Every turn (UserPromptSubmit)** — `amygdala_hook` polls `GET /signal`; if active, injects `BROADCAST [CRITICAL/NUCLEAR]`. `brain_adapter.maybe_refresh()` re-fetches `/feed` every 30 turns (or on hash change).
- **Stop / SubagentStop** — `brain_writer_hook` scans your transcript (forked subprocess, 60s timeout) for `@lesson/@milestone/@signal/@decision` markers and POSTs each to `/marker` with an idempotency key. You write markers in your output; the hook posts them.
- **Throughout** — content-hash dedup (`broadcast.json`), 120s min-interval throttle, halt-phrase rewriting, empty-body suppression. You will never see noise from these.

Implication: do not curl brain-api directly, do not write to `~/.agentihooks/`,
do not call `brain_refresh` reflexively. The hook layer already handles it.

## 7. Broadcasts — Inbound Awareness

`BROADCAST` blocks in your context come from two channels you subscribe to:

- **`brain`** — produced by `brain_adapter`, sourced from `GET /feed`. Hot arcs, inject blocks, operator intent, tick diff, active signals.
- **`amygdala`** — produced by `amygdala_hook`, sourced from `GET /signal`. Emergency severity only (nuclear/critical).

Read every broadcast — it is what the fleet knows right now. Subscribed
channels are set globally via `AGENTIHOOKS_BASE_CHANNELS=brain,amygdala`.

Producing broadcasts (rare): `channel_publish` for coordination, never for
knowledge. Full protocol: see `rules/03-brain-broadcasts.md`.

## 8. Constraints

- **No direct vault writes.** The vault lives on `anton-prod` NFS. You do not see it locally. Writes go through `brain_ingest` MCP or `@marker` comments only.
- **No curl/HTTP to brain-api.** Use MCP tools. If a capability is missing, add it as an MCP tool in `services/mcp/app/tools/`; do not work around with curl.
- **Max 5 markers per session.** Quality over quantity. No marker beats a low-quality one.
- **Halt-phrases are rewritten.** Phrases like "do you understand", "stop all tool calls" are scrubbed from brain content before injection — they will not trigger operator-behavior STOP rules inside ingested text.
- **No live-patching the brain.** Behavior changes to brain services go through the standard code path: edit → commit → push to `dev` → CI → ArgoCD. The base profile (`anton`) enforces this.
- **The vault schema is owned by the kernel.** Do not invent new top-level vault directories; use `raw/inbox/` and let the tick classify.

## 9. Tick Cadence — When Your Writes Show Up

The tick is the brain's maintenance loop. Five phases (scan → reason → signal
→ edge → write), runs in two modes:

- **Scheduled** — CronJob every 2 hours at HH:07 UTC. Always runs.
- **On-demand** — `POST /tick` writes a request file to NFS; `tick-drain` CronJob drains every 2 minutes. Use this when you have just ingested something and need it visible before the next scheduled tick.

Your `brain_ingest` and `@marker` writes appear in `brain-feed/` (and hence
in the next session's `BROADCAST` blocks) at the next tick — within 2 min if
on-demand, within 2 h otherwise.

Phase detail, what each phase writes, post-tick events (Redis, ClickHouse,
embeddings refresh): see `rules/05-tick-cadence.md`.
