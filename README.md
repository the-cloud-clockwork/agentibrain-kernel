# agentibrain-kernel

> A standalone brain + knowledge-base kernel for Claude Code agent fleets. Bring your own vault, your own LLM keys, your own embeddings. Runs on a laptop, a server, or a Kubernetes cluster.

`agentibrain` is a pillar of the **agenti ecosystem** alongside
[`agenticore`](https://github.com/The-Cloud-Clockwork/agenticore) ·
[`agentihooks`](https://github.com/The-Cloud-Clockwork/agentihooks) ·
[`agentibridge`](https://github.com/The-Cloud-Clockwork/agentibridge) — and the **brain layer** every other pillar plugs into. It is self-contained: ships its own services, Helm charts, brain-keeper agent definition, and brain profile overlays, so any fleet of Claude Code agents can read from and write back to a single HTTP brain.

---

## Why

AI agents have no long-term memory. Every session boots blind and forgets everything it learned the moment it exits. The usual fix — "ask the model to remember" — leaks state into prompts, burns tokens, and can't be shared across sessions.

**agentibrain is the filesystem-first alternative.** Memory lives outside the model, in a structured markdown vault you can open in Obsidian. A scheduled *tick* (deterministic + LLM-assisted) writes hot arcs, signals, decay, and synthesis into the vault. A single HTTP kernel fans that vault out to every agent in your fleet over a tiny REST contract.

Five services, one vault, one HTTP contract — no AWS lock-in, no proprietary storage, no vendor SDK.

---

## What you get

```
                  Your agent fleet
   (agenticore pods · Claude Code on laptop · cron jobs)
                        │
                        ▼  HTTP + Bearer auth
   ┌────────────────────────────────────────────────┐
   │              agentibrain-kernel                 │
   │                                                 │
   │   kb-router    obsidian-    embeddings   mcp    │
   │   :8103        reader       :8102        :8104  │
   │                :8101                            │
   │                                                 │
   │           tick-engine (cron + amygdala)         │
   └────────────────────────────────────────────────┘
                        │
              ┌─────────┼──────────┐
              ▼         ▼          ▼
          your vault  postgres   redis
          (markdown)  pgvector   streams
```

| Service | Port | Role |
|---|---|---|
| **kb-router** | 8103 | Brain HTTP contract (`/feed /signal /marker /tick /ingest`) + federated search |
| **obsidian-reader** | 8101 | Read-only vault access (list, read, search, bounded inbox writes) |
| **embeddings** | 8102 | pgvector wrapper — `/embed`, `/search`, OpenAI-compatible |
| **mcp** | 8104 | MCP retrieval tools — `kb_search`, `kb_brief`, `brain_search_arcs`, `brain_get_arc` |
| **tick-engine** | — | Hybrid 2-hour tick (deterministic clustering + optional LLM synthesis) |

Plus an **opt-in `brain-keeper`** agent (ops oracle for triage, enrichment, replay) and **six Helm charts** for Kubernetes (`kb-router`, `obsidian-reader`, `embeddings`, `mcp`, `brain-cron`, `brain-keeper`).

---

## Install

### 1. Laptop (Docker Compose)

```bash
git clone https://github.com/The-Cloud-Clockwork/agentibrain-kernel.git
cd agentibrain-kernel
./local/bootstrap.sh           # writes .env (random tokens) + scaffolds ./vault
docker compose up -d           # 8 containers come up
```

> **Note:** the default Compose stack **builds the 5 service images locally** from `services/*/Dockerfile` on first run (~5 min). To pull pre-built images instead, see [Images & forking](#images--forking).

Smoke test:

```bash
TOK=$(grep ^KB_ROUTER_TOKEN .env | cut -d= -f2)
curl -H "Authorization: Bearer $TOK" http://localhost:8103/feed | jq .
```

You should see `hot_arcs`, `inject_blocks`, `entries`. On a fresh vault these arrays start mostly empty — they fill as ticks run and as you write markers.

Add a local LLM (Ollama, no API key needed):

```bash
docker compose -f compose.yml -f local/compose.ollama.yml up -d
docker compose exec ollama ollama pull llama3.2
```

Three more inference overlays in [`examples/compose/`](examples/compose/) — Ollama, OpenAI direct, LiteLLM gateway. Full local guide: [`local/README.md`](local/README.md).

### 2. Server (Docker Compose, headless)

Same `compose.yml` works on any Linux box with Docker. Bind the vault to a real path, expose `8103` behind your reverse proxy of choice (Traefik, Caddy, nginx), point your fleet at it via `BRAIN_URL`. No Kubernetes required.

### 3. Kubernetes (Helm)

Six charts ship in [`helm/`](helm/) — `kb-router`, `obsidian-reader`, `embeddings`, `mcp`, `brain-cron`, `brain-keeper`. The first five depend on `tcc-k8s-service-template:0.3.8` (vendored as `.tgz` under each chart's `charts/` for offline install). `brain-cron` is a custom 3-template chart for the CronJob + amygdala consumer.

#### Step 1 — Scaffold the vault on persistent storage

```bash
pip install ./agentibrain-kernel       # or `pip install agentibrain` once on PyPI
brain scaffold --vault /mnt/<your-export>
```

Idempotent. Or copy `agentibrain/templates/vault-layout/` straight onto the volume — same result.

#### Step 2 — Provision Secrets

**Path A — plain Opaque Secret (simplest, no External Secrets Operator):**
```bash
./local/k8s-bootstrap.sh --apply -n <your-namespace>
```
Creates three Secrets (`agentibrain-router-secrets`, `embeddings-secrets`, `agenticore-secrets`) with random tokens. Tokens persist in `local/.k8s-tokens` for re-use.

**Path B — External Secrets Operator (GitOps-tracked):**
Wire your secrets manager (OpenBao / AWS Secrets Manager / Vault) via an ESO `ClusterSecretStore`, then set `externalSecret.enabled: true` in your values overlay. Full walkthrough: [`docs/SECRETS.md`](docs/SECRETS.md).

#### Step 3 — `helm install` the six charts

```bash
helm install agentibrain-kb-router       ./helm/kb-router        -f values-kb-router.yaml
helm install agentibrain-obsidian-reader ./helm/obsidian-reader  -f values-obsidian-reader.yaml
helm install agentibrain-embeddings      ./helm/embeddings       -f values-embeddings.yaml
helm install agentibrain-mcp             ./helm/mcp              -f values-mcp.yaml
helm install agentibrain-brain-cron      ./helm/brain-cron       -f values-brain-cron.yaml   # singleton, deploy ONCE per cluster
helm install agentibrain-brain-keeper    ./helm/brain-keeper     -f values-brain-keeper.yaml # OPTIONAL ops oracle (see note below)
```

Each `values-*.yaml` overlay sets:
* `extraVolumes` → NFS server + path or PVC claim for the vault from step 1
* `env.variables.INFERENCE_URL` + `BRAIN_CLASSIFY_MODEL` + `BRAIN_BRIEF_MODEL` → your LLM gateway + model names
* `secrets.external.secretRef` → the Secret from step 2

Sample overlays + ArgoCD `Application` CRs ship in [`examples/`](examples/) — copy, replace every `<your-*>` placeholder, deploy.

> **Note on `brain-keeper`** — this StatefulSet is the optional ops-oracle agent for triage / enrichment / replay. It runs the [`agenticore`](https://github.com/The-Cloud-Clockwork/agenticore) image, which is built and published by that **upstream repo, not by this kernel**. The brain functions fully without `brain-keeper` — skip the chart if you don't need it, or fork agenticore and override `image.repository` to deploy under your own namespace.

#### Optional — ArgoCD instead of `helm install`

Same outcome, declarative. Copy `examples/argocd/` into your platform repo, swap placeholders, `kubectl apply -f` the `agentibrain-root.yaml`. ArgoCD picks up the per-service Apps and the chart sources point back at this kernel repo via multi-source.

Once running, every agent in your fleet gets two env vars and consumes the brain over HTTP:

```yaml
BRAIN_URL: http://agentibrain-kb-router.<your-namespace>.svc:8080
KB_ROUTER_TOKEN: <from-the-secret-in-step-2>
```

Architecture reference: [`docs/architecture/ARCHITECTURE.md`](docs/architecture/ARCHITECTURE.md). Generic deployment guide: [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md).

---

## Connect Claude Code

After install, register the kernel's MCP server with Claude Code so the agent can reach the brain via four tools (`kb_search`, `kb_brief`, `brain_search_arcs`, `brain_get_arc`).

### Laptop (Docker Compose)

Add to `~/.claude/.mcp.json` or your project-local `.mcp.json`:

```json
{
  "mcpServers": {
    "agentibrain": {
      "url": "http://localhost:8104/mcp",
      "headers": {
        "Authorization": "Bearer ${MCP_PROXY_API_KEY}"
      }
    }
  }
}
```

Get the bearer value from `.env`:

```bash
grep ^MCP_PROXY_API_KEY .env
```

Restart Claude Code, then verify with `/mcp` — the `agentibrain` server should appear with 4 tools (`mcp__agentibrain__kb_search`, etc.).

### Kubernetes (agent-mode pod)

For Claude Code running in agent mode inside a pod, point at the in-cluster Service URL of the `mcp` chart you deployed in step 3 above:

```json
{
  "mcpServers": {
    "agentibrain": {
      "url": "http://agentibrain-mcp.<your-namespace>.svc:8080/mcp",
      "headers": {
        "Authorization": "Bearer ${MCP_PROXY_API_KEY}"
      }
    }
  }
}
```

Inject `MCP_PROXY_API_KEY` via `envFrom: secretRef:` from the K8s Secret backing the `mcp` chart (`agentibrain-mcp-secrets` by default).

Full reference (incl. LiteLLM gateway path): [`docs/MCP.md`](docs/MCP.md).

---

## Images & forking

The kernel publishes 5 service images via GitHub Actions. **Standard consumers don't build anything** — pull and go.

| Image | Source | Tag |
|---|---|---|
| `ghcr.io/the-cloud-clockwork/agentibrain-kb-router` | `services/kb-router/` | `:dev`, `:latest` |
| `ghcr.io/the-cloud-clockwork/agentibrain-obsidian-reader` | `services/obsidian-reader/` | `:dev`, `:latest` |
| `ghcr.io/the-cloud-clockwork/agentibrain-embeddings` | `services/embeddings/` | `:dev`, `:latest` |
| `ghcr.io/the-cloud-clockwork/agentibrain-tick-engine` | `services/tick-engine/` | `:dev`, `:latest` |
| `ghcr.io/the-cloud-clockwork/agentibrain-mcp` | `services/mcp/` | `:dev`, `:latest` |

CI: [`.github/workflows/docker-build.yml`](.github/workflows/docker-build.yml) runs on every push to `dev` (→ `:dev`) and `main` (→ `:latest`).

| Path | Builds locally? | Pulls from GHCR? |
|---|:---:|:---:|
| `docker compose up -d` (default) | ✅ first run, ~5 min | ❌ |
| Helm charts | ❌ | ✅ all 5 service images |
| Air-gapped install | ✅ via your registry mirror | n/a |

**For forkers:** push to your fork's `dev` or `main`, the same workflow runs under your namespace and publishes to `ghcr.io/<your-org>/agentibrain-*`. Edit each chart's `image.repository` (or your values overlay) to point at your namespace. The vendored `tcc-k8s-service-template-0.3.8.tgz` makes `helm install` work offline; refresh from upstream with `helm dep update helm/<chart>`.

The `agenticore` image used by the optional `brain-keeper` chart is built by the [`agenticore`](https://github.com/The-Cloud-Clockwork/agenticore) repo, not here.

---

## HTTP contract

Bearer auth via `KB_ROUTER_TOKEN` on every endpoint. Base URL below is `$BRAIN_URL`.

### `GET /feed` — hot arcs + inject blocks

```bash
curl -s "$BRAIN_URL/feed" -H "Authorization: Bearer $KB_ROUTER_TOKEN"
```

```json
{
  "hot_arcs":      [ { "id", "title", "content", "priority", "ttl", "severity" }, ... ],
  "inject_blocks": [ ... ],
  "entries":       [ ... ],
  "generated_at":  "2026-04-27T18:08:00+00:00",
  "hash":          "c4d87ac3f961be48",
  "entry_count":   5
}
```

Cached server-side for `FEED_CACHE_TTL_SECONDS` (default 30s). Read on every agent's SessionStart.

### `GET /signal` — current amygdala alert

Empty `amygdala-active.md` → `{ "active": false, ... }`. Dedup via `hash`.

### `POST /marker` — emit a brain marker

| Type | Routes to | Mode |
|---|---|---|
| `lesson` | `left/reference/lessons-YYYY-MM-DD.md` | append |
| `milestone` | `left/projects/<source>/BLOCKS.md` if dir exists, else `daily/YYYY-MM-DD.md` | append |
| `signal` | `amygdala/<timestamp>-<severity>-<slug>.md` | new file |
| `decision` | `left/decisions/ADR-NNNN-<slug>.md` (auto-numbered) | new file |

```bash
curl -s -X POST "$BRAIN_URL/marker" \
  -H "Authorization: Bearer $KB_ROUTER_TOKEN" \
  -H "Content-Type: application/json" \
  -H "X-Idempotency-Key: session-abc-first-lesson" \
  -d '{"type":"lesson","content":"NFS dirs need 777 for UID 1000 writers","attrs":{"source":"deploy"}}'
```

Idempotency-key window 1h (configurable via `IDEMPOTENCY_TTL_SECONDS`). Replay returns the original response with `idempotent_replay: true`.

### `POST /tick` — request a manual brain tick

File-protocol: writes a request to `brain-feed/ticks/requested/`. The tick-engine picks it up and moves it to `completed/` or `failed/`. Poll `GET /tick/{job_id}`.

### `POST /ingest` — universal ingest

Free-text in. The model named in `BRAIN_CLASSIFY_MODEL` classifies via your inference gateway (any OpenAI-compatible — see [`docs/GATEWAY-CONTRACT.md`](docs/GATEWAY-CONTRACT.md)), fans URLs/repos/files to artifact-store, drops a markdown note in `raw/inbox/`. Spec: [`api/openapi.yaml`](api/openapi.yaml).

### `POST /index_artifact` — sole brain-side embedding write

Per-artifact embedding write surface. Called by ingest pipelines after artifact-store accepts a blob. The artifact-store no longer auto-embeds (brain-blind boundary, 2026-04-26) — every embed flows through this endpoint.

---

## Vault schema

Obsidian-compatible folder tree, writable by humans and by kernel services. `brain scaffold` is the authoritative writer of the schema marker; `local/bootstrap.sh` invokes it on first run.

```
<vault>/
  .brain-schema           # version marker (JSON)
  README.md  CLAUDE.md    # vault rules for AI agents

  # Cognitive regions (owned by tick-engine + daemons)
  raw/{inbox,articles,media,transcripts}/
  clusters/               # canonical arc storage
  brain-feed/             # /feed reads here, /tick writes ticks/requested/
  amygdala/               # /marker type=signal lands here
  frontal-lobe/{conscious,unconscious}/
  pineal/                 # joy + breakthrough region

  # Knowledge base (operator owns — agents curate)
  identity/               # who you are — root node
  left/                   # technical hemisphere — projects, research, reference, decisions, incidents
  right/                  # creative hemisphere — ideas, strategy, life, creative, risk
  bridge/                 # cross-hemisphere synthesis
  daily/                  # append-only daily logs

  templates/mubs/         # VISION SPECS BLOCKS TODO STATE BUGS KNOWN-ISSUES ENHANCEMENTS MVP PATCHES
```

Scaffold is idempotent. Schema-version mismatch is a hard error unless `--force-upgrade` is passed. Full reference: [`docs/VAULT-SCHEMA.md`](docs/VAULT-SCHEMA.md).

---

## Configuration

| Env var | Default | Purpose |
|---|---|---|
| `VAULT_ROOT` | `/vault` | Vault mount path inside containers |
| `KB_ROUTER_TOKEN` / `KB_ROUTER_TOKENS` | — | Bearer auth (single token or comma-sep list) |
| `OBSIDIAN_READER_URL` | `http://obsidian-reader:8080` | Reader service URL |
| `EMBEDDINGS_URL` | `http://embeddings:8080` | Embeddings service URL |
| `INFERENCE_URL` | — | OpenAI-compatible LLM gateway. Empty = deterministic-only ticks. See [`docs/GATEWAY-CONTRACT.md`](docs/GATEWAY-CONTRACT.md) |
| `INFERENCE_API_KEY` | — | Bearer token for the inference gateway. Empty = no auth header (trusted-LAN ok) |
| `BRAIN_CLASSIFY_MODEL` | `brain-classify` | Model name for kb-router classifier |
| `BRAIN_BRIEF_MODEL` | `brain-brief` | Model name for `kb_brief` / tick synthesis |
| `LLM_API_KEY` | — | API key for embeddings (optional — empty disables semantic search) |
| `MCP_PROXY_API_KEY` | — | Bearer mcp-proxy enforces on inbound calls |
| `FEED_CACHE_TTL_SECONDS` | `30` | `/feed` cache window |
| `IDEMPOTENCY_TTL_SECONDS` | `3600` | `/marker` replay window |
| `TICK_INTERVAL_SECONDS` | `7200` | Tick cadence (compose mode) |

Local mode reads from `.env` (generated by `bootstrap.sh`). K8s mode reads from a `Secret` (Opaque or ESO-synced from your secret store — see [`docs/SECRETS.md`](docs/SECRETS.md)).

---

## Observability

The kernel ships a starter Grafana dashboard at [`observability/brain-health.json`](observability/brain-health.json) — drop it into Grafana to get an immediate picture of the brain's pulse: hot arcs, emergency signals, broadcast traffic, tick cadence, memory markers, and hook health. 27 panels across six brain regions (frontal lobe · amygdala · broadcast cortex · pineal · hippocampus · hook observability), so the layout maps onto the same terminology the kernel uses internally.

![Brain dashboard](observability/brain-dashboard.png)

**How to wire it.** The JSON is a mock — every panel queries a `grafana-clickhouse-datasource` with `uid: clickhouse`, against the `brain.*` schema that [`services/tick-engine`](services/tick-engine) writes into ClickHouse on every tick (`brain.tick_health`, `brain.signals`, `brain.arcs`, `brain.lessons`, `brain.embeddings`, …). To go from mock to live:

1. **Import** — in Grafana, *Dashboards → New → Import* → paste `observability/brain-health.json`.
2. **Datasource** — install the [ClickHouse datasource plugin](https://grafana.com/grafana/plugins/grafana-clickhouse-datasource/), point it at the ClickHouse instance the tick-engine writes to, and either name its uid `clickhouse` or remap the dashboard's datasource at import time.
3. **Schema** — the queries assume the tick-engine's default table layout. If you've renamed tables or split databases, edit the panel `rawSql` blocks — column names match the `BrainTickHealth` model in [`services/tick-engine/brain_tick.py`](services/tick-engine/brain_tick.py).
4. **Refresh** — default cadence is `30s` over a `now-6h` window; override per your appetite.

If you don't run ClickHouse, the JSON is still useful as a panel layout reference — swap each `rawSql` for the equivalent in your TSDB of choice and keep the structure.

---

## Development

```bash
git clone https://github.com/The-Cloud-Clockwork/agentibrain-kernel
cd agentibrain-kernel
python -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'

pytest tests/unit                              # scaffold + compose tests
PYTHONPATH=services/kb-router:. pytest services/kb-router/tests -q   # 25 service tests

docker build -t agentibrain-kb-router:local services/kb-router
```

Workflow: `dev` is the working branch; PRs go `dev` → `main`. CI on `main` ships `:latest` GHCR images automatically. PyPI publish ([`.github/workflows/publish.yml`](.github/workflows/publish.yml)) fires on `v*.*.*` tag push.

---

## Status

**v0.1.1 — first stable.** Six Helm charts. Five service images auto-published to GHCR (`:dev` from dev branch, `:latest` from main). HTTP contract frozen at v1. Generic OpenAI gateway — kernel speaks chat-completions to any compatible upstream (LiteLLM, OpenAI, Ollama, vLLM, …). Brain-blind boundary in place since 2026-04-26 (artifact-store no longer auto-embeds; every embed flows through `POST /index_artifact`).

The kernel is self-contained and the canonical source of truth for everything brain-related — services, Helm charts, brain-keeper agent definition (`agents/brain-keeper/`), brain profile overlays (`profiles/brain/`, `profiles/brain-keeper/`), and the vault layout schema. All deployment-specific plumbing (cluster namespaces, model name aliases, secret-store paths, NFS hosts) lives in your own platform repo, not here.

Maturity tracking lives in [`operator/`](operator/):
- [`operator/VISION.md`](operator/VISION.md) — what 100% means
- [`operator/STATE.md`](operator/STATE.md) — current snapshot
- [`operator/BLOCKS.md`](operator/BLOCKS.md) — in-flight work
- [`operator/ENHANCEMENTS.md`](operator/ENHANCEMENTS.md) — backlog
- [`operator/TODO.md`](operator/TODO.md) — next actions

---

## Further reading

- [`docs/architecture/ARCHITECTURE.md`](docs/architecture/ARCHITECTURE.md) — full kernel design
- [`docs/architecture/CLUSTERS.md`](docs/architecture/CLUSTERS.md) — arc lifecycle
- [`docs/architecture/KEEPER.md`](docs/architecture/KEEPER.md) — brain-keeper agent
- [`docs/architecture/MARKERS.md`](docs/architecture/MARKERS.md) — marker grammar
- [`docs/architecture/SYMBIOSIS.md`](docs/architecture/SYMBIOSIS.md) — relation to agenticore + agentihooks
- [`docs/architecture/TELEMETRY.md`](docs/architecture/TELEMETRY.md) — OTel + Langfuse
- [`docs/MCP.md`](docs/MCP.md) — MCP server, Claude Code wiring, LiteLLM gateway
- [`docs/SECRETS.md`](docs/SECRETS.md) — Opaque Secrets vs ESO
- [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) — multi-source ArgoCD pattern
- [`docs/VAULT-SCHEMA.md`](docs/VAULT-SCHEMA.md) — vault layout v1
- [`api/openapi.yaml`](api/openapi.yaml) — HTTP contract

---

## License

[MIT](LICENSE).
