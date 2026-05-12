# CLAUDE.md — agentibrain-kernel

## Identity

You are working in **agentibrain-kernel**, the standalone brain + KB substrate
for the agenti ecosystem.

## Architecture (target: 5 workloads, 4 images)

The brain system follows a 3-operation model: **ingest, read, update**.

### Services (anton-dev namespace)

| Service | Image | Role | What it does |
|---|---|---|---|
| **brain-api** | `agentibrain-brain-api` | Ingest + Read | HTTP API: vault read/write, ingest pipeline, search, feed, markers, tick trigger. Mounts NFS vault directly. |
| **mcp** | `agentibrain-mcp` | Read (MCP) | MCP protocol adapter for Claude Code sessions. Calls embeddings + brain-api + LiteLLM. |
| **embeddings** | `agentibrain-embeddings` | Index | Vector store (pgvector). Embed, search, prune. |
| **brain-keeper** | `agenticore` | Maintain | Autonomous agent for vault maintenance (different runtime). |

### Ops workloads (anton-ops namespace)

| Workload | Type | Role | What it does |
|---|---|---|---|
| **brain-ops** | CronJob (2h) | Update | Full 5-phase brain tick: scan, reason, signal, edge, write. |
| **tick-drain** | CronJob (2m) | Update | On-demand tick queue drain (polls NFS requested/ dir). |
| **amygdala** | Deployment | Alert | Redis Streams consumer, broadcasts severity alerts. |

### Data flow

```
Claude Code → LiteLLM MCP proxy → mcp → embeddings (semantic) + brain-api (vault text) + LiteLLM (AI)
brain-api POST /tick → NFS requested/ → tick-drain → brain_tick.py → vault + ClickHouse
brain-ops (every 2h) → same tick pipeline, scheduled
amygdala → Redis streams (anton:events:*) → fleet alerts
```

### Shared data stores

- **NFS vault** (10.10.30.130): brain-api, brain-ops mount it. obsidian-reader removed (absorbed into brain-api).
- **Postgres/pgvector**: embeddings service only
- **LiteLLM** (anton-dev): brain-api, mcp, brain-ops for AI inference
- **Redis**: brain-ops writes events, amygdala reads them
- **ClickHouse**: brain-ops writes tick metrics

### Simplification in progress

Naming convention: `agentibrain-{role}` everywhere. Tracked in plan at
`.claude/plans/reactive-plotting-wall.md`. Phase 1 (vault_reader absorption)
is deployed. obsidian-reader removed — brain-api
now serves vault read/write directly via `/vault/*` endpoints.

## Scope

This repo owns:
- Brain services: brain-api (brain-api + vault-reader), mcp, embeddings, brain-ops
- Helm charts: brain-ops, brain-keeper, embeddings, brain-api, mcp
- The brain-keeper agent definition (single source of truth)
- Brain profile overlays for agentihooks
- The vault layout schema and the `brain scaffold` tool that writes it
- The HTTP API contract (`api/openapi.yaml`)
- The Python CLI (`brain init|up|down|status|tick|scaffold|version`)

## What this repo does NOT own

- `artifact-store` / `artifact-transform` — general storage plane, lives in your downstream platform repo
- Generic Claude Code hooks — those live in `agentihooks` (this kernel exposes HTTP, hooks talk to it)
- `broadcast.py` / `channels.py` — fleet coordination, stays in agentihooks
- Deployment values files, secret-store paths, operator-specific paths — stay in your downstream platform repo (antoncore)

## Core principle

**Two contracts, nothing else:**
1. The kernel exposes an HTTP API. No shared filesystem paths across boundaries.
2. The vault layout is a versioned schema owned by the kernel. `brain scaffold`
   is the only authoritative writer.

## Versioning

The `version` field in `pyproject.toml` is managed by the release workflow. Do
not edit it manually. The runtime version lives in `agentibrain/__init__.py`
(`__version__`).

## Working model

Dev-first flow:
- Work on `dev` → PR to `main` → merge.
- `main` is the release trunk; CI publishes `:latest` images on every merge.
- `dev` carries the in-flight changes; CI publishes `:dev` images on every push.
- Image builds go to `ghcr.io/the-cloud-clockwork/agentibrain-*`.

## Downstream consumers

- Downstream platform repos (antoncore) — use the kernel's Helm charts with environment-specific values.
- `agentihub` — clones `agents/brain-keeper/` at install time.
- `agentihooks-bundle` — clones `profiles/brain/` and `profiles/brain-keeper/` at install time.
- External users — `git clone` → `./local/bootstrap.sh` → `docker compose up -d` (or use the Helm charts for K8s).
