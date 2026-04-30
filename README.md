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
docker compose up -d           # 8 containers come up
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

### 3. Kubernetes (Helm)

Charts ship in [`helm/`](helm/):

```
helm/agentibrain-kb-router/
helm/agentibrain-obsidian-reader/
helm/agentibrain-embeddings/
helm/agentibrain-brain-cron/
helm/agentibrain-brain-keeper/
```

All five inherit from `tccw-k8s-service-template v0.3.4`. Consume them as ArgoCD Applications with a values overlay, or `helm install` directly. Architecture reference: [`docs/architecture/ARCHITECTURE.md`](docs/architecture/ARCHITECTURE.md).

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

Free-text in. Haiku classifies, fans URLs/repos/files to artifact-store, drops a markdown note in `raw/inbox/`. Spec: [`api/openapi.yaml`](api/openapi.yaml).

---

## Vault schema

Obsidian-compatible folder tree, writable by humans and by kernel services. `local/bootstrap.sh` scaffolds the schema marker on first run.

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
| `INFERENCE_URL` | — | LLM proxy URL. Empty = deterministic-only ticks |
| `LLM_API_KEY` | — | API key for embeddings + inference (optional) |
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

Branching: `dev` is the working branch. PRs go `dev` → `main`. `main` ships `:latest` images and PyPI releases on tag push.

---

## Status

**v0.1.1 — first stable.** Dev + prod deploys live, brain-blind boundary verified end-to-end (2026-04-27). Five Helm charts shipped, brain-keeper agent definition canonical, HTTP contract frozen at v1. The kernel is the **canonical and exclusive** source of truth for everything brain-related — downstream consumers (`agentihub`, `agentihooks-bundle`, `antoncore`) carry no vendored copies.

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
