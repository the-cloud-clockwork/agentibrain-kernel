# Brain Clusters — Semantic Memory Compartments

> **Author:** Nestor Colt
> **Date:** 2026-04-09
> **Status:** Living vision — cluster primitive + amygdala/pineal/frontal-lobe regions
> **Complements:** `operator/references/OBSIDIAN-BRAIN.md` (left/right hemisphere architecture)

---

## 1. The Mental Model

The operator is a parallel-task human. Eight Claude Code sessions running at once. Attention snaps between technical deep-dives, creative bursts, infrastructure debugging, vision sessions. Linear journals and flat note hierarchies lose the shape of that work — you get a pile of bullets with no story.

A **cluster** (aka **arc**) is the structural primitive that preserves shape:

- A cluster is a compartmentalized unit of attention — a semantic, time-bounded task-in-action with a clear ignition, a trajectory, and (eventually) a resolution.
- Clusters are nodes. They connect to other clusters via edges (the Obsidian graph).
- The AI extracts clusters from raw session history and places them in the right brain region.
- The operator moves between clusters with zero friction — each cluster has enough context to re-enter instantly without re-reading the whole session.

This is frictionless context-switching for a power user. The AI keeps the ledger; the operator keeps flow.

### Arcs vs clusters — terminology

**Cluster** is the technical storage primitive. **Arc** is the user-facing narrative name. The terms are interchangeable; use "arc" when talking to humans, "cluster" in tool names, schemas, and code.

Think *video-game story arc*: a journey with an ignition, a trajectory, a resolution, and optional connections to other arcs.

- A **single-session cluster** is a **solo arc**.
- A **chain of connected clusters** linked via `parent`/`child` edges inside a continuous time window forms a **story arc** (or mega-arc). Brain-keeper can optionally auto-generate a top-level arc file that wikilinks the member clusters as one narrative unit.
- The Obsidian graph view visualises arcs as nodes and story arcs as dense sub-graphs.

In practice: every cluster file *is* an arc. When you say "the LiteLLM self-service arc", you mean the cluster file or the connected chain — the reader resolves it from context.

---

## 2. The Cluster Primitive — Schema

Every cluster is a single markdown file with YAML frontmatter. No exceptions.

```markdown
---
cluster_id: 2026-04-09-litellm-mcp-self-service
title: LiteLLM MCP Self-Service Arc
region: left-hemisphere          # left | right | amygdala | pineal | frontal-lobe | raw
status: complete                 # active | complete | stalled | abandoned
ignition: anton-router agent gap — needed runtime fallbacks + capability flags
started_at: 2026-04-08T01:08:00Z
ended_at: 2026-04-09T18:25:00Z
duration_hours: 41.3
tags: [litellm, mcp, unit-system, router-agent, hardening]
edges:
  - 2026-04-07-agenticore-unit-debate
  - 2026-04-09-openbao-provider-rename
  - 2026-04-09-traefik-middleware-cleanup
source_sessions:
  - 7d031027-5bdc-478b-a558-442ac37ec5a0
  - a69e27d7-cc96-4831-b570-15bb3a8798ad
heat: 9                          # 0–10, decides frontal-lobe promotion
---

# LiteLLM MCP Self-Service Arc

## Ignition
- anton-router agent onboarding surfaced 5 gaps: capability flags, provider secrets, playground, inference logs, routing/fallbacks
- Pre-existing 18 raw LiteLLM tools were too granular — agents shouldn't call `create_key` / `set_mcp_tool_permissions` directly
- Mother thread: `7d031027` on 2026-04-06 → workflow-tool philosophy established

## Timeline
### ✅ Completed
1. Unit system + litellm-state repo (PR #270, mother session)
2. Provider-aware onboarding — 5 tools (PR #274)
3. Model invocation — 2 tools (PR #276)
4. Inference logs — 1 tool (PR #277)
5. Model health & routing — 4 tools, built by Opus sub-agent A in worktree (PR #278)
6. Rate limits & guardrails — 5 tools, built by Opus sub-agent B in worktree (PR #278)
7. OpenBao rename: `OPENAI_DIRECT_API_KEY` → `OPENAI_API_KEY` (convention enforcement)
8. Traefik middleware cleanup: dropped `mcp-wellknown-rewrite`

### ❌ Errors / learning
- PR #275 merge conflict: post-squash divergence from PR #274. **Lesson**: after squash-merge to main, NEVER cherry-pick onto old dev tip — always fresh branch from main. Used this pattern for PRs #276, #277, #278 successfully.
- Dev image tag `:dev` only builds from dev branch pushes. **Lesson**: ship to main → cherry-pick onto dev to trigger `:dev` image rebuild.
- In-session MCP tool catalog frozen at start. **Lesson**: after adding a new tool, operator must `/mcp` reload for this session to see it.

### 🏁 Resolution
- 26 → 35 MCP tools on both dev + prod
- 10 categories, full self-service surface for anton-router agent
- Zero raw LiteLLM API calls required from any agent
- 8 PRs merged cleanly (#270, #271, #272, #273, #274, #276, #277, #278)

## Lessons to reproduce
- Fresh branch from main → cherry-pick → PR workflow avoids squash divergence
- Parallel Opus sub-agents in worktrees with different insertion anchors → merge-clean
- Provider env var naming strict `{PROVIDER_UPPER}_API_KEY` → convention beats inventory tool
- Capability flags must live in `model_info` at registration time — retrofitting is painful

## Edges
- [[2026-04-07-agenticore-unit-debate]] — parent. Where the unit system was born.
- [[2026-04-09-openbao-provider-rename]] — spawned from this cluster, standalone sub-arc.
- [[2026-04-09-traefik-middleware-cleanup]] — hardening side-quest during a compact break.

## Source sessions
- home-bridge://sessions/7d031027-5bdc-478b-a558-442ac37ec5a0 (mother thread, 3367 turns)
- home-bridge://sessions/a69e27d7-cc96-4831-b570-15bb3a8798ad (execution thread, 2216 turns)
```

### Mandatory fields
- `cluster_id` — slug, date-prefixed
- `region` — which brain region this cluster lives in
- `status` — lifecycle state
- `ignition` — one-line trigger
- `source_sessions` — always at least one session UUID for traceback

### Optional but valued
- `heat` (0–10) — used by the frontal lobe to decide promotion
- `edges` — graph connectivity (wikilinks in body do the same but frontmatter is machine-readable)
- `duration_hours` — for cadence analysis

---

## 3. New Brain Regions

The existing vision (`operator/references/OBSIDIAN-BRAIN.md`) defines `left/`, `right/`, `bridge/`, `raw/`, `daily/`. This document **adds three endocrine-inspired regions** for emotional gating and executive focus.

### 3.1 Amygdala — `amygdala/` + fleet broadcast

**Function**: fight-or-flight **broadcast**. The amygdala is NOT just a folder. It is a real-time, fleet-wide alarm system. When a critical incident fires, every running agent (Claude Code sessions, agenticore agents, OpenClaw agents) receives the alarm within seconds via the agentihooks broadcast layer — then reacts. The folder is the persistent artifact; the broadcast is the live signal.

**Two layers**:

1. **Broadcast layer (real-time)** — agentihooks emits a `brain_amygdala_alarm` event on:
   - Telegram (anton-agent bot posts to the operator channel)
   - `notifications` MCP (operator phone push)
   - Redis broadcast stream `amygdala:alarms` on DB 11
   All running sessions + agents subscribe via a SessionStart hook. On receive, the alarm is injected as a `<system-reminder>` into the agent's next turn.

2. **Persistence layer (learning)** — every alarm also writes a cluster markdown file to `amygdala/<iso>-<slug>.md` for post-incident analysis. These files feed the self-improvement loop and the operator profile.

**Severity levels**:
| Level | Agent reaction |
|---|---|
| `alert` | Surface in the agent's next turn. No work pause. |
| `urgent` | Surface in next turn. Agents halt non-critical side quests. |
| `nuclear` | All agents pause current work immediately and wait for operator ack. Production deploy failure, suspected compromise, credential exposure, full-cluster outage. Boom, and all of them boom boom shut down. |

**Auto-firing signal sources**:
- Production incidents: ArgoCD prod sync failures, CrashLoopBackOff on `anton-prod`, k8s node NotReady
- Supply chain attacks (e.g. LiteLLM v1.82.7 PyPI malware)
- Credentials in context (accidental plaintext in any agent turn)
- Burn-rate alarms, quota overruns (LiteLLM `/spend/logs` clustered 429s)
- Data-loss risks, deletions without backup
- Legal / compliance flags
- OpenBao audit anomalies
- Cloudflare WAF block spikes

**Rules**:
- Every alarm lands with `status: active` until the operator explicitly resolves OR the fire condition stops for 15 minutes (auto-downgrade)
- Alarms still `active` at 24h auto-escalate via `notifications` MCP with higher priority
- An amygdala cluster can **graduate** to left/right hemisphere once the incident is closed — keeps the learning record without the stress flag
- The amygdala cluster file is the persistent record; the broadcast is the live signal. Losing one does not invalidate the other.

### 3.2 Pineal Gland — `pineal/`

**Function**: joy, excitement, discovery. Where the AI stores clusters that made the operator happy — good ideas, eureka moments, unexpected wins, things that triggered "this is it".

**What lands here**:
- Breakthrough architecture ideas
- First-time-working validations ("it actually responded")
- Unexpected pattern discoveries
- Creative sparks that haven't matured into projects yet
- Satisfying cleanup sessions (sensory: the "swept kitchen" feeling)

**Detection signals** (heuristics for the extractor):
- Operator messages with exclamations, `finally`, `this is it`, `beautiful`, `perfect`
- Correction-free arcs (no friction corrections in the cluster)
- Short-duration wins (cluster completed in under 2h with full success)

**Rules**:
- Pineal clusters are **append-only reference material**. Never mark as stalled or abandoned.
- Used by the brain-keeper agent to surface "you did this kind of work and loved it" on slow days
- Feeds the identity/about-me.md update loop — what pattern of work genuinely energizes the operator

### 3.3 Frontal Lobe — `frontal-lobe/`

**Function**: executive decision-making. The Wall Street dashboard. What matters RIGHT NOW for the next decision the operator has to make.

**NOT raw material** — raw stays in `raw/`. Frontal lobe is a curated, time-sensitive, small surface of the hottest, most-referenced clusters.

**Two subdivisions**:

1. **`frontal-lobe/conscious/`** — actively in the operator's awareness. Things the operator has explicitly referenced in the last session. Current projects, open decisions, things on the TODO surface.
2. **`frontal-lobe/unconscious/`** — digested patterns the brain-keeper agent has extracted from recent clusters but that the operator hasn't verbalized. Quiet inference. Things the AI believes the operator cares about based on what they've touched.

**Promotion rules**:
- A cluster is promoted to frontal-lobe when `heat >= 7` AND (last access within 7 days OR status is active)
- Promotion is **by reference, not copy** — the frontal-lobe file is a symlink-equivalent (markdown alias with `![[cluster-slug]]` embed)
- Demotion happens after 14 days of no access OR explicit operator command
- Capacity cap: max 20 clusters in `conscious/`, max 40 in `unconscious/`. Overflow triggers a forced demotion of the lowest-heat entry.

**Layout**:
```
frontal-lobe/
├── _dashboard.md       # Auto-generated index. Up-arrows and down-arrows per cluster (weekly delta in heat).
├── conscious/
│   ├── 2026-04-09-litellm-mcp-self-service.md
│   ├── 2026-04-09-brain-clusters-vision.md
│   └── ...
└── unconscious/
    ├── 2026-04-07-agenticore-same-image-pattern.md
    └── ...
```

The `_dashboard.md` is the "Wall Street ticker" — one line per cluster showing heat, status, last-touched, delta.

---

## 4. Flow — Raw → Region → Frontal Lobe

```
┌─────────┐
│  raw/   │  Session jsonl files, unstructured ingest
└────┬────┘
     │  Cluster extractor (scheduled or /brain-clusters skill)
     ▼
┌─────────────────────────────────────────────┐
│  Semantic clustering (AI)                    │
│  - group related sessions by task arc        │
│  - infer ignition, timeline, resolution      │
│  - detect emotional signals (amygdala/pineal)│
│  - compute heat score                        │
└─────┬───────────┬───────────┬────────────────┘
      ▼           ▼           ▼
┌──────────┐ ┌──────────┐ ┌──────────┐
│   left/  │ │  right/  │ │ amygdala │   (initial placement)
│          │ │          │ │  pineal/ │
└────┬─────┘ └────┬─────┘ └────┬─────┘
     │            │             │
     │   heat >= 7 & recent?    │
     ▼            ▼             ▼
          ┌─────────────────┐
          │  frontal-lobe/  │  (promotion by reference)
          │   conscious/    │
          │   unconscious/  │
          └─────────────────┘
```

**Key**: placement is **hemispheric** (left/right/amygdala/pineal/bridge). Promotion is **frontal lobe overlay**. A cluster lives in its hemisphere forever; the frontal lobe just references it during its hot period.

---

## 5. Edges — The Graph

Clusters are nodes. Edges are typed:

| Edge type | Meaning |
|---|---|
| `parent` | This cluster spawned from another. Required when an arc continues from a previous cluster. |
| `child` | This cluster birthed a sub-arc. Mirror of parent. |
| `sibling` | Same time window, same operator, different task. (Parallel session pattern.) |
| `unblocks` | Completing this cluster unblocked another. |
| `supersedes` | This cluster replaces/invalidates another. |
| `related` | Loose topical connection. |

Edges live both in YAML frontmatter (`edges:` list with typed entries) and as `[[wikilinks]]` in the body. Brain-keeper agent ensures they stay in sync.

---

## 6. Cadence — What the Extractor Measures

For each cluster, the extractor computes:

- **Arc length**: duration from ignition to resolution
- **Interruption count**: how many unrelated sessions the operator switched to during this arc (parallel-task pressure)
- **Friction ratio**: (corrections + errors) / total turns — low is smooth, high is hard-won
- **Tool churn**: how many distinct MCP tools touched — indicates scope breadth
- **Compaction events**: how many `/compact` commands happened mid-arc (context stress signal)

These go in cluster frontmatter as `cadence: {length_h, interruptions, friction, tool_churn, compactions}` and feed operator-profile updates.

---

## 7. Integration with Existing Obsidian Vision

The cluster primitive is **orthogonal** to MUBS (Minimal Unit of Brain Storage, defined in `OBSIDIAN-BRAIN.md`).

- **MUBS** = container for an ongoing project/idea (VISION.md, SPECS.md, BLOCKS.md, etc.)
- **Cluster** = historical record of a task-in-action that touched one or more MUBS instances
- Clusters reference MUBS via `edges: [mubs:left/projects/antoncore]`
- A MUBS can list recent clusters in its `BLOCKS.md` under "Recent arcs"

**Extraction sources**:
- Primary: `~/.claude/projects/*/<session>.jsonl` (Claude Code transcripts)
- Secondary: `home-bridge` MCP sessions index
- Future: Codex/Cursor session logs via an adapter

**Target vault paths** (once operator green-lights the layout):
```
vault/
├── left/clusters/<YYYY-MM>/<slug>.md
├── right/clusters/<YYYY-MM>/<slug>.md
├── amygdala/<slug>.md                   # flat, not month-nested — urgency means shallow
├── pineal/<YYYY-MM>/<slug>.md
├── frontal-lobe/
│   ├── _dashboard.md
│   ├── conscious/<slug>.md              # ![[cluster-original]] embeds
│   └── unconscious/<slug>.md
└── raw/sessions/<YYYY-MM-DD>/<session-id>.jsonl     # optional archival of source
```

---

## 8. Alternative Backends

Obsidian is the current substrate because it's trending and the operator is trying it. This architecture is **substrate-agnostic**:

- **Obsidian** (now): markdown + YAML + wikilinks → Obsidian graph view
- **RAG store** (future): chunks indexed by region + heat, retrieval filtered by cluster_id
- **Vector DB + graph DB hybrid**: embeddings for semantic recall, graph for edge traversal, frontal lobe as a "hot set" shard

The cluster schema and the extraction skill don't care which backend is active. Swap `target_path` for `vector_index` and the pipeline is the same.

---

## 9. Operator Identity Anchoring

Three things stay constant regardless of which region a cluster lives in:

1. **Every cluster traces back to a session UUID** — zero orphans
2. **Every cluster has an ignition line** — the WHY is mandatory
3. **Every cluster carries a heat score** — the frontal lobe needs it

If the AI can't compute these three for a candidate cluster, the candidate stays in `raw/` and gets flagged for operator review.

---

## 10. First Exemplar

The first real cluster is **this conversation itself**: `2026-04-09-litellm-mcp-self-service.md` (see Section 2 for the full example). It was extracted by hand in the current session via home-bridge MCP tool calls. The `/brain-clusters` skill automates exactly that pattern.
