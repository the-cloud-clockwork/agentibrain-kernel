# CLAUDE.md — agentibrain-kernel

## Identity

You are working in **agentibrain-kernel**, the standalone brain + KB substrate
for the agenti ecosystem.

## Architecture (target: 5 workloads, 4 images)

The brain system follows a 3-operation model: **ingest, read, update**.

### Services (deployed in your prod namespace)

| Service | Image | Role | What it does |
|---|---|---|---|
| **brain-api** | `agentibrain-brain-api` | Ingest + Read | HTTP API: vault read/write, ingest pipeline, search, feed, markers, tick trigger. Mounts NFS vault directly. |
| **mcp** | `agentibrain-mcp` | Read (MCP) | MCP protocol adapter for Claude Code sessions. Calls embeddings + brain-api + LiteLLM. |
| **embeddings** | `agentibrain-embeddings` | Index | Vector store (pgvector). Embed, search, prune. |
| **brain-keeper** | `agenticore` | Maintain | Autonomous agent for vault maintenance (different runtime). |

### Ops workloads (deployed in your ops namespace)

| Workload | Type | Role | What it does |
|---|---|---|---|
| **brain-ops** | CronJob (2h) | Update | Full 5-phase brain tick: scan, reason, signal, edge, write. |
| **tick-drain** | CronJob (1m) | Update | On-demand tick queue drain: coalesces pending requests by kind, runs the tick, then refreshes the semantic index. |
| **amygdala** | Deployment | Alert | Redis Streams consumer, broadcasts severity alerts. |

### Data flow

```
Claude Code → LiteLLM MCP proxy → mcp → embeddings (semantic) + brain-api (vault text) + LiteLLM (AI)
brain-api POST /tick → NFS requested/ → tick-drain → brain_tick.py → vault + ClickHouse
brain-ops (every 2h) → same tick pipeline, scheduled
amygdala → Redis streams (EVENT_BUS_STREAM, configurable) → fleet alerts
```

### Shared data stores

- **NFS vault**: brain-api, brain-ops mount it. obsidian-reader removed (absorbed into brain-api). Server address is deploy-time config.
- **Postgres/pgvector**: embeddings service only
- **LiteLLM**: brain-api, mcp, brain-ops for AI inference (URL via `LLM_API_BASE` / `INFERENCE_URL`)
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
- Deployment values files, secret-store paths, operator-specific paths — stay in your downstream platform repo

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
- `dev` is the working branch; CI publishes `:dev` images on every push to dev.
- `main` is the **snapshot branch**: reached only by a reviewed `dev` → `main`
  PR, it publishes no image and deploys nothing. Treat it as a checkpoint log.
- Image builds go to `ghcr.io/the-cloud-clockwork/agentibrain-*`. `:dev` is the
  only tag CI publishes — there is no `:latest`.

## Redeploying after a code change

**Code is the source of truth.** A change reaches a running brain only by
rebuilding or redeploying from source. Editing a file inside a running
container leaves the deployment out of sync with the repo and is undone by the
next restart — do not do it, on either path below.

Which path applies is decided by how this checkout is running, so check before
acting:

```bash
docker compose ps 2>/dev/null | grep -q agentibrain && echo "COMPOSE" || echo "not compose"
kubectl get statefulset -l app.kubernetes.io/part-of=agentibrain -A 2>/dev/null
```

### Path A — Docker Compose (laptop / single server)

Compose builds from this source tree. `docker compose up -d` alone reuses the
existing image and keeps running the old code — `--build` is the step that
makes a change take effect.

```bash
git pull
docker compose up -d --build            # rebuild + recreate changed services
docker compose ps                       # confirm healthy
```

Rebuild only what changed when you know the blast radius:

| Changed | Rebuild |
|---|---|
| `services/brain-api/` | `brain-api` |
| `services/embeddings/` | `embeddings` |
| `services/mcp/` | `mcp` |
| `services/brain-ops/` | `tick-cron` `tick-drain` `amygdala` (one shared image) |

Verify the container is younger than the pull — a cached layer that silently
survived is the failure mode worth checking for:

```bash
docker compose ps --format 'table {{.Service}}\t{{.RunningFor}}'
```

Volumes and the vault bind-mount survive `down` / `up --build`. Only
`docker compose down -v` destroys them.

### Path B — Kubernetes

Do not `helm upgrade` from a laptop and do not edit live resources. Push to
`dev`; CI builds `:dev` and the cluster's image updater rolls the pods.

```bash
git add -A && git commit -m "..." && git push origin dev
gh run watch                            # CI build
kubectl -n <your-namespace> rollout status statefulset/<name>
```

Chart or values changes follow the same route — commit them, let GitOps
reconcile.

### Verifying either path

A redeploy is done when the new behaviour is observed against the running
service, not when the push succeeded. Name the check and run it:

```bash
curl -H "Authorization: Bearer $KB_ROUTER_TOKEN" $BRAIN_URL/health
docker compose logs --since 2m <service>     # or: kubectl logs
```

## Downstream consumers

- Downstream platform repos — use the kernel's Helm charts with environment-specific values.
- `agentihub` — clones `agents/brain-keeper/` at install time.
- `agentihooks-bundle` — clones `profiles/brain/` and `profiles/brain-keeper/` at install time.
- External users — `git clone` → `./local/bootstrap.sh` → `docker compose up -d` (or use the Helm charts for K8s).
