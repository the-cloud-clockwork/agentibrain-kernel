# agentibrain-kernel

> A standalone brain + knowledge-base kernel for AI fleets. Bring your own vault, your own LLM keys, your own embeddings. Runs on a laptop, a server, or a Kubernetes cluster. Built to be installed by friends, not just by its author.

`agentibrain` is a pillar of the **agenti ecosystem** alongside
[`agenticore`](https://github.com/The-Cloud-Clock-Work/agenticore) ·
[`agentihooks`](https://github.com/The-Cloud-Clock-Work/agentihooks) ·
[`agentibridge`](https://github.com/The-Cloud-Clock-Work/agentibridge) ·
[`agentihub`](https://github.com/The-Cloud-Clock-Work/agentihub) — and the **brain layer** every other pillar plugs into. It packages everything that used to live scattered across those repos into one pluggable memory + KB substrate that any fleet of Claude Code / Codex / Gemini agents can read from and write back to.

---

## Why

AI agents have no long-term memory. Every session boots blind and forgets everything it learned the moment it exits. The usual fix — "ask the model to remember" — leaks state into prompts, burns tokens, and can't be shared across sessions.

**agentibrain is the filesystem-first alternative.** Memory lives outside the model, in a structured markdown vault you can open in Obsidian. A scheduled *tick* (deterministic + LLM-assisted) writes hot arcs, signals, decay, and synthesis into the vault. A single HTTP kernel fans that vault out to every agent in your fleet over a tiny REST contract.

It's opinionated. Dual-hemisphere split (technical vs. creative). MUBS (Minimal Unit of Brain Storage) everywhere. Obsidian-compatible out of the box. Five services, one vault, one HTTP contract — no AWS lock-in, no proprietary storage, no vendor SDK.

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
| **mcp** | 8104 | MCP surface for `kb_search` / `kb_brief` / `kb_dispatch` |
| **tick-engine** | — | Hybrid 2-hour tick (deterministic clustering + optional LLM synthesis) |

Plus an opt-in **brain-keeper** agent (ops oracle for triage, enrichment, replay) and a full set of Helm charts for the Kubernetes path.

---

## Install — pick your friction

### 1. Laptop (Docker Compose) — 3 commands

```bash
git clone https://github.com/The-Cloud-Clock-Work/agentibrain-kernel.git
cd agentibrain-kernel
./local/bootstrap.sh           # writes .env (random tokens) + scaffolds ./vault
docker compose up -d           # 10 containers come up
```

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

Full local guide — inference modes, port overrides, vault bind to your existing Obsidian, troubleshooting — in [`local/README.md`](local/README.md).

### 2. Server (Docker Compose, headless)

Same `compose.yml` works on any Linux box with Docker. Bind the vault to a real path, expose `8103` behind your reverse proxy of choice (Traefik, Caddy, nginx), point your fleet at it via `BRAIN_URL`. No Kubernetes required.

### 3. Kubernetes (Helm) — three steps

Charts ship in [`helm/`](helm/) — `kb-router`, `obsidian-reader`, `embeddings`, `brain-cron`, `brain-keeper`. All five inherit from `tccw-k8s-service-template v0.3.6` (v1beta1 ESO).

#### Step 1 — Scaffold the vault on persistent storage

The vault is a markdown directory tree (~30 folders + ~52 templates + a `.brain-schema` marker). Pick where it lives — NFS export, PVC, or hostpath — then scaffold it:

```bash
pip install ./agentibrain-kernel       # or pip install agentibrain when on PyPI
brain scaffold --vault /mnt/<your-export>
```

Idempotent. Or copy `agentibrain/templates/vault-layout/` straight onto the volume — same result.

#### Step 2 — Drop a Secret in your cluster

Required keys (operator picks naming):

| Key | Purpose |
|---|---|
| `KB_ROUTER_TOKEN` | Bearer for the HTTP contract |
| `INFERENCE_API_KEY` | LLM gateway auth (LiteLLM virtual key, OpenAI key, empty for trusted-LAN Ollama) |
| `LLM_API_KEY` | Embeddings provider auth (optional — empty disables semantic search) |

Either via your secret manager + ESO `ClusterSecretStore` (recommended — see `docs/SECRETS.md`) or a plain Opaque Secret.

#### Step 3 — `helm install` the five charts

```bash
helm install agentibrain-kb-router       ./helm/kb-router        -f values-kb-router.yaml
helm install agentibrain-embeddings      ./helm/embeddings       -f values-embeddings.yaml
helm install agentibrain-obsidian-reader ./helm/obsidian-reader  -f values-obsidian-reader.yaml
helm install agentibrain-brain-keeper    ./helm/brain-keeper     -f values-brain-keeper.yaml
helm install agentibrain-brain-cron      ./helm/brain-cron       -f values-brain-cron.yaml   # singleton, deploy ONCE per cluster
```

Each `values-*.yaml` overlay sets:
* `extraVolumes` → NFS server + path or PVC claim for the vault from step 1
* `env.variables.INFERENCE_URL` + `BRAIN_CLASSIFY_MODEL` + `BRAIN_BRIEF_MODEL` → your LLM gateway + model names
* `secrets.external.secretRef` → the Secret from step 2

Sample overlays + ArgoCD `Application` CRs ship in [`examples/`](examples/) — copy, replace every `<your-*>` placeholder, deploy.

#### Optional — ArgoCD instead of `helm install`

Same outcome, declarative. Copy `examples/argocd/` into your platform repo, swap placeholders, `kubectl apply -f` the `agentibrain-root.yaml`. ArgoCD picks up the per-service Apps and the chart sources point back at this kernel repo.

Once running, every agent in your fleet gets two env vars and consumes the brain over HTTP:

```yaml
BRAIN_URL: http://agentibrain-kb-router.<your-namespace>.svc:8080
KB_ROUTER_TOKEN: <from-the-secret-in-step-2>
```

Architecture reference: [`docs/architecture/ARCHITECTURE.md`](docs/architecture/ARCHITECTURE.md). Generic deployment guide: [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md).

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

Free-text in. The model named in `BRAIN_CLASSIFY_MODEL` classifies via your inference gateway (any OpenAI-compatible — see [GATEWAY-CONTRACT.md](docs/GATEWAY-CONTRACT.md)), fans URLs/repos/files to artifact-store, drops a markdown note in `raw/inbox/`. Spec: [`api/openapi.yaml`](api/openapi.yaml).

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
| `INFERENCE_URL` | — | OpenAI-compatible LLM gateway. Empty = deterministic-only ticks. See [GATEWAY-CONTRACT.md](docs/GATEWAY-CONTRACT.md) |
| `INFERENCE_API_KEY` | — | Bearer token for the inference gateway. Empty = no auth header (trusted-LAN ok) |
| `BRAIN_CLASSIFY_MODEL` | `brain-classify` | Model name for kb-router classifier. See [brain-models.yaml](operator/brain-models.yaml) |
| `BRAIN_BRIEF_MODEL` | `brain-brief` | Model name for kb_brief / tick synthesis |
| `LLM_API_KEY` | — | API key for embeddings (optional) |
| `FEED_CACHE_TTL_SECONDS` | `30` | `/feed` cache window |
| `IDEMPOTENCY_TTL_SECONDS` | `3600` | `/marker` replay window |
| `TICK_INTERVAL_SECONDS` | `7200` | Tick cadence (compose mode) |

Local mode reads from `.env` (generated by `bootstrap.sh`). K8s mode reads from a `Secret` synced by ExternalSecrets Operator from your secret store.

---

## Development

```bash
git clone https://github.com/The-Cloud-Clock-Work/agentibrain-kernel
cd agentibrain-kernel
python -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'

pytest tests/unit                              # scaffold + compose tests
PYTHONPATH=services/kb-router:. pytest services/kb-router/tests -q   # 25 service tests

docker build -t agentibrain-kb-router:local services/kb-router
```

Branching: `dev` is the working branch. PRs go `dev` → `main`. `main` ships `:latest` GHCR images. PyPI publish (`publish.yml`) is wired and dormant — fires on `v*.*.*` tag push when external distribution is needed; descoped from current critical path since the fleet consumes the kernel via Helm + image, not pip.

---

## Status

**v0.1.1 — first stable.** Dev + prod deploys live, brain-blind boundary in place since 2026-04-26 (artifact-store no longer auto-embeds — every embed flows through `POST /index_artifact`). HTTP contract frozen at v1. Generic OpenAI gateway contract — the kernel speaks chat-completions to any compatible upstream (LiteLLM, OpenAI, Ollama). Five Helm charts shipped, generic `examples/` tree on-board for forkers (8 sample value overlays + 10 ArgoCD `Application` CRs + root). The kernel is the **canonical and exclusive** source of truth for everything brain-related — downstream consumers (`agentihub`, `agentihooks-bundle`, `antoncore`) carry no vendored copies.

Decoupling cutover (2026-04-30 → 2026-05-03): all operator-specific deployment plumbing (anton namespaces, claude-max-* model names, OpenBao paths, NFS hosts) lives in the operator's platform repo. Kernel is generic and clone-and-deploy.

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
- [`docs/VAULT-SCHEMA.md`](docs/VAULT-SCHEMA.md) — vault layout v1
- [`api/openapi.yaml`](api/openapi.yaml) — HTTP contract

---

## License

[MIT](LICENSE).
