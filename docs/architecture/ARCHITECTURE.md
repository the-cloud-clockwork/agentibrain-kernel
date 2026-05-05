---
title: Overview
parent: Architecture
nav_order: 1
---

# Brain System — Memory Nervous System

The brain system is the memory layer for an AI agent fleet. It captures the operator's work as structured **arcs** (narrative units with ignition, timeline, resolution, and edges), compartmentalizes them by region and heat, and injects the hottest context into every agent session at startup.

Design principle: **80% deterministic, 20% AI.** Parsing, heat computation, file promotion, and feed generation are pure Python (~5ms). Edge discovery, merge suggestions, signal escalation, and intent inference are LLM (~24s, ~$0.05). The deterministic layer prepares a compressed context; the AI layer reasons over it.

---

## Architecture — The Hybrid Tick

```
brain_tick.py (full cycle, ~24s)
│
├── Phase 1: brain_keeper.py ──────── 1ms, $0.00
│   Parse vault → compute heat → promote/demote → write brain-feed
│
├── Phase 2: brain_tick_prompt.py ─── 4ms, $0.00
│   Compress arc table + edge map + signals + lessons into AI prompt
│
├── Phase 3: inference-gateway ────── ~24s, ~$0.05
│   Sonnet reasons: missing edges, merge/split, signal escalation, intent
│
├── Phase 4: brain_apply.py ───────── 5ms, $0.00
│   Write recommendations to vault: edges, merges, signal updates, intent
│
└── Phase 5: verify ───────────────── 1ms, $0.00
    Re-scan vault to confirm changes persisted
```

The tick runs as a K8s CronJob every 2h at HH:07 UTC (12 ticks/day). Full extraction (extract.py + cluster.py) runs once daily at 04:07 UTC; other ticks run brain_keeper.py + brain_tick.py (deterministic + AI reasoning) only. The brain-keeper agent pod is deployed but the maintenance tick is deterministic Python, NOT an LLM agent loop.

**Why not pure LLM:** First attempt used a Sonnet agent to do the entire tick. Result: 9.5 minutes, $2.16, 80 turns, FAILED. The hybrid approach: 24 seconds, $0.05, succeeded, changes verified.

---

## Components

### 1. Tick Engine (`services/tick-engine/`)

Python scripts, pure stdlib on the runtime side (no pip dependencies beyond `redis` for the amygdala consumer), packaged as `ghcr.io/the-cloud-clockwork/agentibrain-tick-engine:latest` (built from `services/tick-engine/`).

| Script | Purpose | Time |
|--------|---------|------|
| `extract.py` | Scan `~/.claude/projects/*/*.jsonl`, filter by time + project, output JSON bundle | <1s |
| `cluster.py` | Group sessions into clusters, assign region + heat + deterministic cluster_id | <1s |
| `markers.py` | Parse YAML frontmatter + inline `<!-- @type -->` HTML comment markers from any markdown file | <1ms |
| `brain_keeper.py` | Vault maintenance: heat recomputation, promotion/demotion, brain-feed generation | 1-5ms |
| `brain_tick_prompt.py` | Build compressed AI prompt from pre-computed vault state | 4ms |
| `brain_apply.py` | Parse AI reasoning output, apply recommendations to vault files | 5ms |
| `brain_tick.py` | Orchestrator: chains all 5 phases into one hybrid tick | ~24s total |

```bash
# Deterministic only (no AI, no cost)
python3 brain_tick.py --vault /vault --brain-feed /vault/brain-feed --no-ai

# Full hybrid tick (deterministic + AI reasoning + apply + verify)
python3 brain_tick.py --vault /vault --brain-feed /vault/brain-feed

# Dry run (no writes)
python3 brain_tick.py --vault /vault --brain-feed /vault/brain-feed --dry-run
```

### 2. Marker Protocol

Documents use HTML comment markers that are invisible in Obsidian/GitHub but parseable by regex. Two audiences: deterministic parsers (grep/Python) and LLMs (semantic anchors).

**Inline markers:**
```markdown
<!-- @hot heat=9 region=left -->
This paragraph is extractable without LLM.
<!-- @/hot -->

<!-- @lesson -->
Always write LITELLM_KEY unconditionally — silent failures are the worst.
<!-- @/lesson -->

<!-- @signal severity=critical source=auth-broker -->
Auth broker ban rate increasing — amygdala candidate.
<!-- @/signal -->

<!-- @decision date=2026-04-10 -->
Use deterministic parsers over LLM agents for structured data extraction.
<!-- @/decision -->

<!-- @edge type=parent target=2026-04-09-artifact-platform -->

<!-- @inject target=claude.md -->
Brain MVP is the top priority.
<!-- @/inject -->

<!-- @todo priority=1 -->
First operator-profile corpus run.
<!-- @/todo -->
```

**Marker types:** `@hot`, `@lesson`, `@signal`, `@decision`, `@edge`, `@inject`, `@todo`

**Why HTML comments:** Obsidian renders them invisible (clean reading). GitHub ignores them. Single regex: `<!-- @(\w+)(.*?)-->`. Block regex with DOTALL captures content. No conflict with markdown syntax. LLMs see them in raw text as semantic anchors.

**Current vault stats:** 18 arcs retrofitted with 58 markers. brain_keeper.py extracts 44 lessons, 5 signals, 1 inject block in 47ms.

### 3. Arc Schema

Every arc is a markdown file with YAML frontmatter. Schema defined in `docs/brain/CLUSTERS.md` Section 2.

```yaml
---
cluster_id: 2026-04-09-litellm-mcp-self-service   # deterministic SHA1 hash
title: LiteLLM MCP Self-Service Arc
region: left-hemisphere          # left | right | amygdala | pineal | frontal-lobe
status: complete                 # active | complete | stalled | abandoned
heat: 9                          # 0-10, decides frontal-lobe promotion
source_sessions:
  - 7d031027-5bdc-478b-a558-442ac37ec5a0
  - a69e27d7-cc96-4831-b570-15bb3a8798ad
synthesized: true                # false = stub, true = Timeline/Lessons/Resolution filled
---
```

**Arc terminology:** "cluster" = technical storage primitive, "arc" = user-facing narrative name. Interchangeable. Use "arc" with humans, "cluster" in code.

### 4. Vault Compartments

The brain lives in the Obsidian vault on shared storage at `<your-vault-path>/`:

| Compartment | Path | Purpose | Population |
|---|---|---|---|
| Clusters | `clusters/<YYYY-MM-DD>/` | Canonical arc storage, date-grouped | 18+ arcs |
| Frontal Lobe (conscious) | `frontal-lobe/conscious/` | Hot arcs (heat ≥ 7), auto-injected | 8 arcs |
| Frontal Lobe (unconscious) | `frontal-lobe/unconscious/` | Cooled arcs still linked via edges | 0 (nothing cooled yet) |
| Amygdala | `amygdala/` | Emergency signals | 1 (auth-broker SPOF) |
| Pineal | `pineal/` | Joy + breakthrough arcs | 1 (brain-etl self-referential) |
| Left Hemisphere | `left/` | Graduated technical long-term memory | Existing (projects, research, reference, incidents, decisions) |
| Right Hemisphere | `right/` | Graduated creative/strategic | Existing (ideas, strategy, life, creative, risk) |
| Brain Feed | `brain-feed/` | Agent-readable files for brain_adapter | 5 files |

### 5. Brain Feed Files

Generated by `brain_keeper.py`, read by `brain_adapter` at every agent SessionStart.

| File | Purpose | Priority |
|------|---------|----------|
| `hot-arcs.md` | Top 10 arcs by heat, table format | 10 (highest) |
| `signals.md` | Active signals from `@signal` markers (tombstoned signals excluded) | 8 |
| `inject.md` | Content from `@inject` markers — meant for CLAUDE.md injection | 9 |
| `intent.md` | AI-inferred operator intent from last tick | 7 |
| `last-tick-diff.md` | What changed in the last tick (edges, merges, signals) | 5 |

**Signal lifecycle (tombstone logic):**
1. AI tick detects resolved issue → `brain_apply.py` sets `severity=resolved` and appends `(CLEARED: reason)` to signal content
2. Next tick: if signal is already `severity=resolved` AND contains `(CLEARED:`, `brain_apply.py` **deletes the entire signal block** (tombstone) from the source arc file
3. `brain_keeper.py` `write_signals_feed` also skips any resolved+CLEARED signals during brain-feed generation
4. Result: resolved signals appear in exactly one tick cycle, then vanish. No infinite accumulation.

**Title truncation:** `write_hot_arcs_md` truncates arc titles to 80 chars and escapes pipe characters to prevent AI reasoning text from leaking into the markdown table.

All files use brain_adapter YAML frontmatter:
```yaml
---
id: hot-arcs-2026-04-10
title: Active Hot Arcs
priority: 10
ttl: 3600
severity: info
---
```

### 6. brain_adapter (Agentihooks Hook)

Pluggable source-to-channel bridge. Reads brain-feed files, publishes to broadcast channel system. Every agent session receives hot arcs + signals + inject blocks automatically.

**Source:** `agentihooks/hooks/context/brain_adapter.py`
**Hook wiring:** `hook_manager.py` → `on_session_start()` calls `inject_on_session_start()`, `on_user_prompt_submit()` calls `maybe_refresh()` (turn-counter gated)
**Change detection:** SHA-256 hash — only republishes when content changes
**Channel MCP tools:** `channel_publish`, `brain_status`, `brain_refresh` in `hooks/mcp/channels.py`

**Config (env vars):**
```yaml
BRAIN_ENABLED: "true"
BRAIN_SOURCE_PATH: "/vault/brain-feed"
BRAIN_CHANNEL: "brain"
BRAIN_REFRESH_INTERVAL: "30"      # turns between refresh checks
```

**Currently wired on:**
- K8s agents: agenticore + your fleet's agents (env vars in Helm values, all in `<your-namespace>`).
- Local fleet (WSL2 / laptop): all repos with `.agentihooks.json` containing `"channels": ["brain", "amygdala"]`. Brain-feed synced via rsync cron (`*/5 * * * *`) from your vault NFS export to `~/.agentihooks/brain-feed/`. Env vars in `~/.claude/settings.json`.

**Channel subscription required:** Broadcast system reads `.agentihooks.json` from project CWD for `channels` array. Without `["brain", "amygdala"]`, brain messages are published but filtered out at delivery.

### 7. brain-cron (K8s CronJob)

Hybrid tick every 2h at HH:07 UTC (`7 */2 * * *`). Full extraction runs once daily at 04:07; every tick runs the 5-phase hybrid pipeline (deterministic → AI prompt → LLM call → apply → verify).

```bash
# What brain-cron runs (every 2h):
# Phase 0 — extraction (04:07 UTC only):
python3 /app/extract.py --since 26h --min-turns 5 \
  --projects-dir /shared/.claude/projects \
  | python3 /app/cluster.py --out-dir /vault/clusters/$(date -u +%Y-%m-%d)
# Phases 1-5 — every tick:
python3 /app/brain_tick.py --vault /vault --brain-feed /vault/brain-feed
```

**Deployment:** `helm/brain-cron/` (CronJob). Operators pick a namespace (e.g. `<your-ops-namespace>`).
**Image:** `ghcr.io/the-cloud-clockwork/agentibrain-tick-engine:latest`
**Vault mount:** RW at `/vault` (NFS or PVC — see `helm/brain-cron/values.yaml`). Shared-FS at `/shared` (RO) is optional and only needed when co-located with agenticore runtime pods.

### 8. brain-keeper (K8s StatefulSet)

Agenticore instance deployed for the 20% AI tasks (edge discovery, synthesis, complex reasoning). The deterministic tick (`brain_keeper.py`) runs in brain-cron; the AI tick dispatches to this pod.

**Deployment:** `helm/brain-keeper/` (StatefulSet; operators pick a namespace).
**Profile:** `profiles/brain-keeper/` (kernel canonical — agentihooks-bundle clones at install).
**Agent definition:** `agents/brain-keeper/` (kernel canonical — agentihub clones at install).
**LiteLLM:** operators register the agent as a model via `add_agent_as_model`. Tool count and model choice are operator policy.
**Model:** Sonnet 4.6 (default; override via `AGENT_MODE_MODEL` env).

### 9. Brain ETL (Session-Scope Loop)

In-session cron for continuous monitoring during active operator sessions. Launches sub-agents to harvest operator state + cross-session activity, synthesizes arcs, writes to vault.

**Artifacts:** `brain-etl/INSTRUCTIONS.md` (self-contained tick brief), `brain-etl/LEARNINGS.md` (append-only audit log)
**History:** Ran 20 ticks overnight (2026-04-09/10), validated empty-delta pattern, discovered that sub-agent A thrashes on >8 file reads

### 10. Health Tracking

Every hybrid tick records a health score (1-10) to `brain-feed/health.jsonl`:

```json
{"timestamp": "2026-04-10T12:03:00Z", "score": 6, "reason": "edges underconnected, duplicate arcs exist", "arcs": 19, "signals": 5, "lessons": 53}
```

Time series enables: "Is the brain getting healthier over time?" Track coverage, freshness, connectivity, signal quality.

---

## The Hybrid Tick in Detail

### What the deterministic layer does (80%)
- Parse all arc files in `vault/clusters/` — YAML frontmatter + inline markers
- Compute heat scores — arithmetic formula: recency + tool volume + session count + status bonus
- Promote arcs with heat ≥ 7 to `frontal-lobe/conscious/`
- Demote arcs with heat < 5 to `frontal-lobe/unconscious/`
- Generate `brain-feed/hot-arcs.md` — top 10 arcs by heat
- Collect all `@signal` markers → `brain-feed/signals.md`
- Collect all `@inject` markers → `brain-feed/inject.md`
- Update `_dashboard.md` per date directory

### What the AI layer does (20%)
- **Edge discovery:** "These two arcs are related but have no edge — add one"
- **Merge/split:** "These arcs are duplicates — merge them" / "This arc is too broad — split it"
- **Signal escalation:** "This warning should be critical" / "This signal was fixed — clear it"
- **Operator intent:** "Based on heat distribution, the operator is working on X"
- **Brain health:** "6/10 — edges are sparse, two duplicate arcs exist"

### What NEITHER layer does (deferred)
- Arc replay (Block 4 — semantic recall + workflow reproduction)
- Amygdala broadcast via Redis Streams (Block 3 — fleet-wide agent halt)
- Automated arc-to-artifact pipeline (Publisher auto-generates from hot arcs)

---

## Agent Wiring

### Helm values (per agent)
```yaml
env:
  variables:
    BRAIN_ENABLED: "true"
    BRAIN_SOURCE_PATH: "/vault/brain-feed"
    BRAIN_CHANNEL: "brain"
# Operator-supplied vault mount — pick NFS (example below) or a PVC.
extraVolumes:
- name: brain
  nfs:
    server: <your-nfs-host>
    path: <your-vault-path>/brain-feed
    readOnly: true
extraVolumeMounts:
- name: brain
  mountPath: /vault/brain-feed
  readOnly: true
```

### Agents with brain injection
| Agent | BRAIN_ENABLED | NFS mount | Verified |
|---|---|---|---|
| agenticore | true | /vault/brain-feed | ✓ SessionStart injection confirmed |
| your router agent | true | /vault/brain-feed | ✓ |
| publisher | true | /vault/brain-feed | ✓ |
| brain-keeper | true | /vault/brain-feed + full /vault (RW) | ✓ |

---

## Vault storage

Operators pick one of:

- **NFS share** — one export for the full vault (RW) that the tick-engine
  CronJob and brain-keeper StatefulSet both mount at `/vault`. Brain-feed is a
  subtree; agents can mount just the subtree read-only. Example exports:
  ```
  "/path/to/vault"            -async,no_subtree_check,fsid=301 10.0.0.0/24
  "/path/to/vault/brain-feed" -async,no_subtree_check,fsid=302 10.0.0.0/24
  ```
- **PVC** — a PersistentVolumeClaim with ReadWriteMany. Agent pods can mount
  the same claim read-only by sub-path.
- **Local compose** — `brain up` mounts the host vault directory directly.

---

## File Map

```
services/tick-engine/
├── Dockerfile           ← Python 3.12-slim + 6 scripts
├── extract.py           ← Session scanner (jsonl → JSON bundle)
├── cluster.py           ← Deterministic grouper (bundle → arc stubs)
├── markers.py           ← Marker parser library (frontmatter + @type markers)
├── brain_keeper.py      ← Vault maintenance (heat, promote, brain-feed)
├── brain_tick_prompt.py ← AI prompt builder (compressed context)
├── brain_apply.py       ← AI recommendation applier (edges, merges, signals)
└── brain_tick.py        ← Orchestrator (all phases in one command)

helm/brain-cron/     ← CronJob + amygdala Deployment (HH:07 every 2h)
helm/brain-keeper/   ← StatefulSet chart (agenticore agent for AI tasks)

# ArgoCD Application manifests are operator-specific and live in each
# operator's deployment repo (typical layout: k8s/argocd/{dev,prod}/brain-*.yaml).
```

---

## Tracking & Reference

| Document | Purpose |
|----------|---------|
| `docs/architecture/CLUSTERS.md` | Arc primitive schema (authoritative) |
| `docs/architecture/SYMBIOSIS.md` | Philosophical compass — the WHY |
| `docs/architecture/KEEPER.md` | brain-keeper agent responsibilities + commands |
| `docs/architecture/MATURITY.md` | Maturity scorecard |
| `docs/VAULT-SCHEMA.md` | Folder layout owned by `brain scaffold` |
| `api/openapi.yaml` | HTTP contract |

Operator-specific planning and rollout notes (e.g. `operator/BRAIN-MVP.md`)
live in each operator's deployment repo.
| `brain-etl/LEARNINGS.md` | ETL audit log (overnight run: 20 ticks) |
| `docs/8E-MEMORY.md` | Memory stack hierarchy (auto-memory → AgentiBridge → brain arcs) |
| `docs/9D-OPERATOR-CRONJOBS.md` | brain-cron listed in <your-ops-namespace> workload table |

---

## Relationship to Other Systems

| System | Relationship |
|--------|-------------|
| KB Catalog (`docs/8J-KB-CATALOG.md`) | kb_brief synthesizes arc stubs; kb_search can federate arcs from artifact-store + vault |
| Artifact Platform (`docs/8I-ARTIFACT-PLATFORM.md`) | Publisher uses kb_dispatch to turn hot arcs into media artifacts |
| Agent Prompt RL (`operator/MVP.md`) | Brain arcs = the reward signal RL optimizes against. Brain first, RL second. |
| Event Bus (`docs/7G-EVENTBUS.md`) | Amygdala broadcasts will use Redis Streams (Block 3, pending) |
| Operator Profile | brain-keeper's heat scoring + lessons feed profile quality metrics |
| AgentiBridge | Session indexing feeds extract.py; restore_session enables arc replay |
| Inference Gateway | AI reasoning tick routes through inference-gateway (sonnet + haiku fallback) |

---

## Evolution Notes

- **2026-04-09:** Arc schema + extract.py + cluster.py + vault scaffold + Phase 0 baseline (18 arcs)
- **2026-04-10:** markers.py + brain_keeper.py (47ms deterministic tick) + brain_adapter SessionStart injection verified + 58 markers retrofitted across 18 arcs + brain_tick_prompt.py (hybrid AI reasoning) + brain_apply.py (apply layer) + brain_tick.py (full orchestrator) + first hybrid tick: 6 edges, 3 merges, 3 signal changes, health 6/10, verified persistent
- **2026-04-11:** Block 3 amygdala E2E validated (6s round-trip). Block 5 write path shipped (brain_writer_hook, outbox sync, Redis XADD). Grafana 12-panel dashboard (ClickHouse brain.tick_health). Arc graduation (heat < 2, age > 7d → hemisphere). Ntfy failure notification. Overlay broadcast on profile activate/deactivate. Auto-overlay lifecycle (AGENTIHOOKS_AUTO_OVERLAY env var). brain-tools image rebuilt 5x. 51 duplicate markers + 20 bad edges cleaned.
- **2026-04-12:** Block 4 replay pipeline shipped: extract_workflow.py + embed_arcs.py + brain_search_arcs MCP + /replay skill + replay-edge heat boost. 60 arcs embedded in pgvector (19 dev + 41 prod). E2E replay deferred pending workflow template population.
- **2026-04-13:** 5-layer OTel pipeline shipped (brain.inject / brain.delivery / brain.marker_write spans). Grafana dashboard redesigned to 27 panels across 6 biological regions. brain-keeper evolved from daemon to first-class agent: LiteLLM model + AgentiBridge A2A + Drive report pipeline (md+csv → Google Doc+Sheet). brain-smoke 5/5 PASS.
- **2026-04-14:** Mitigation tombstone (arcs with `mitigates: <source>` auto-clear source signals). Sibling edges auto-wired at extraction time. heal.py 7-point drift audit. reasoner_feedback.py closes the AI feedback loop. Auto-triage daily cron. brain-tools memory limit bumped 4→8Gi.
- **2026-04-15:** 8O Brain Reader's Guide shipped (field manual for dashboard + markers + troubleshooting). OTel blackout fixed (telemetry.py HTTP fallback + settings.json protocol change).
- **2026-04-18:** Documentation audit + consolidation. Maturity re-assessed at ~70% (was 80%). Tick schedule corrected (every 2h, not daily).
- **Pending:** First real /replay E2E, nuclear halt for amygdala, vault backup, broadcast MCP tool, profile activation broadcast, embed_arcs.py scheduling

---

## Dispatched Agent Brain Injection — How It Works

Dispatched agents (via `home-bridge dispatch_task`) receive brain broadcasts if:

1. **`~/.agentihooks/brain-feed/`** has `.md` files (rsync cron `*/5` from vault)
2. **`BRAIN_ENABLED`** auto-detects to `true` when brain-feed dir has files
3. **`BRAIN_SOURCE_PATH`** defaults to `~/.agentihooks/brain-feed/`
4. **`.agentihooks.json`** exists at the dispatched agent's CWD with `"channels": ["brain", "amygdala"]`
5. **SessionStart hook order:** brain_adapter publishes → broadcast injection reads → context injected

**Critical:** Dispatched sessions land in CWD `$HOME/` (the launcher's home dir), NOT the `project` param path. The `project` param sets CLAUDE.md context only. So `$HOME/.agentihooks.json` must exist with channel subscriptions.

Without `.agentihooks.json` at CWD, `register_session()` finds no channels, and all brain/amygdala broadcasts are filtered out.

**Verification:** Dispatch an agent asking what brain content it sees. Should report Hot Arcs, Signals, nuclear, BROADCAST blocks from brain-adapter.

---

## Stress Test Playbook

> Moved to `docs/brain/READERS-GUIDE.md` § Stress Testing. This is operational runbook content, not architecture.
