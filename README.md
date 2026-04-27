# agentibrain-kernel

> A standalone brain + knowledge-base kernel for AI fleets. Bring your own vault, your own LLM keys, your own embeddings. Runs on a laptop or on K8s. Made to be installed by friends, not just its author.

`agentibrain` is the 6th pillar of the **agenti ecosystem** alongside
[`agenticore`](https://github.com/The-Cloud-Clock-Work/agenticore) •
[`agentihooks`](https://github.com/The-Cloud-Clock-Work/agentihooks) •
[`agentibridge`](https://github.com/The-Cloud-Clock-Work/agentibridge) •
[`agentihub`](https://github.com/The-Cloud-Clock-Work/agentihub) •
[`agentipublish`](https://github.com/The-Cloud-Clock-Work/agentipublish).
It packages everything that used to live scattered across those repos as a **brain layer** — a pluggable memory + KB substrate that any fleet of Claude Code / Codex / Gemini agents can read from and write back to.

---

## Why

AI agents have no long-term memory. Every session boots blind and forgets everything it learned the moment it exits. The usual fix — "ask the model to remember" — leaks state into prompts, burns tokens, and can't be shared across sessions.

**agentibrain is the filesystem-first alternative.** Instead of stuffing memory into prompts, agents read it from a structured markdown vault that sits outside the model. The vault is organized by cognitive region (`clusters/`, `frontal-lobe/`, `amygdala/`, `bridge/`), written by a scheduled *tick* (deterministic + LLM-assisted), and served over HTTP to every agent pod in the fleet via a single kernel service.

It's opinionated. Dual-hemisphere split (technical vs. creative). MUBS (Minimal Unit of Brain Storage) everywhere. Obsidian-compatible out of the box. Four services, one CLI, one vault — no AWS lock-in, no proprietary storage.

---

## What you get

```
           ┌──────────────────────────────────────────────────────┐
           │                  Your agent fleet                     │
           │   (agenticore pods, Claude Code on laptop, cron)      │
           └─────────────────────────┬────────────────────────────┘
                                     │  HTTP (Bearer auth)
                                     ▼
   ┌─────────────────────────────────────────────────────────────┐
   │                    agentibrain-kernel                         │
   │                                                               │
   │  ┌────────────┐ ┌────────────┐ ┌────────────┐ ┌───────────┐ │
   │  │ kb-router  │ │ obsidian-  │ │ embeddings │ │ tick-     │ │
   │  │ /ingest    │ │ reader     │ │ pgvector   │ │ engine    │ │
   │  │ /feed      │ │ /list      │ │ /search    │ │ (hybrid   │ │
   │  │ /signal    │ │ /read      │ │ /embed     │ │  2h tick) │ │
   │  │ /marker    │ │ /search    │ │            │ │           │ │
   │  │ /tick      │ │ /write     │ │            │ │           │ │
   │  └─────┬──────┘ └─────┬──────┘ └─────┬──────┘ └─────┬─────┘ │
   └────────┼──────────────┼──────────────┼──────────────┼───────┘
            │              │              │              │
            └──────────────┴──────┬───────┴──────────────┘
                                  ▼
                      ┌───────────────────────┐
                      │   Your vault folder    │
                      │    (Obsidian-         │
                      │     compatible)        │
                      └───────────────────────┘
                                  +
                   Postgres (pgvector) · Redis · optional S3/MinIO
```

Four HTTP services, each single-purpose:

| Service | Port | Role |
|---|---|---|
| **kb-router** | 8080 | Ingest + federated search + brain HTTP contract (`/feed`, `/signal`, `/marker`, `/tick`) |
| **obsidian-reader** | 8080 | Read-only vault access (list, read, search, bounded write-inbox) |
| **embeddings** | 8080 | pgvector wrapper — `/embed`, `/search`, OpenAI-compatible |
| **tick-engine** | — | Cron/on-demand hybrid tick (deterministic clustering + LLM reasoning) |

Plus:

- **brain-keeper** — first-class agent for brain ops (triage, enrichment, replay). Canonical definition lives here; downstream repos (`agentihub`, `agentihooks-bundle`) mirror it.
- **brain profile** + **brain-keeper profile** for agentihooks — the opinionated hook overlay.
- **Vault schema v1** — seeded by `brain scaffold`. Full dual-hemisphere layout + MUBS templates + identity root.

---

## Install

### Local / friend path (Docker Compose)

```bash
git clone https://github.com/The-Cloud-Clock-Work/agentibrain-kernel.git
cd agentibrain-kernel
./local/bootstrap.sh              # generates .env (random tokens) + scaffolds ./vault
docker compose up -d              # 8 containers: 5 brain services + postgres + redis
TOK=$(grep ^KB_ROUTER_TOKEN .env | cut -d= -f2)
curl -H "Authorization: Bearer $TOK" http://localhost:8103/feed | jq .
```

For a local LLM (Ollama, no API key needed):

```bash
docker compose -f compose.yml -f local/compose.ollama.yml up -d
docker compose exec ollama ollama pull llama3.2
```

Full local docs — inference modes, port config, troubleshooting, vault bind to your Obsidian — live in [`local/README.md`](local/README.md).

> **CLI note.** `pip install agentibrain` ships a `brain` CLI that aspires to render the compose for you (`brain init`, `brain up`, `brain scaffold`). The CLI shell exists; the compose-rendering templates are still being wired up. Until then, the static `compose.yml` + `local/bootstrap.sh` above is the supported path.

### Cloud path (bring your own Postgres + S3)

```bash
pip install agentibrain
brain init --vault ~/my-vault \
           --s3-bucket my-brain-bucket \
           --postgres-url postgres://... \
           --openai-key $OPENAI_API_KEY
brain up
brain scaffold
```

### Kubernetes path (Helm)

Charts are shipped in the kernel at `helm/`. See `docs/architecture/ARCHITECTURE.md` for the deploy reference; operators working from the antoncore monorepo consume them via ArgoCD apps under `k8s/argocd/{dev,prod}/agentibrain-*.yaml`.

---

## HTTP contract

All endpoints bearer-auth via `KB_ROUTER_TOKEN`. Base URL in the examples is `$BRAIN_URL`.

### `GET /feed` — hot arcs + inject blocks + operator intent

```bash
curl -s "$BRAIN_URL/feed" -H "Authorization: Bearer $KB_ROUTER_TOKEN"
```

Returns:
```json
{
  "hot_arcs":      [ { "id", "title", "content", "priority", "ttl", "severity" }, ... ],
  "inject_blocks": [ ... ],
  "entries":       [ ... ],
  "generated_at":  "2026-04-22T15:11:28+00:00",
  "hash":          "c4d87ac3f961be48",
  "entry_count":   5
}
```

Cached server-side for `FEED_CACHE_TTL_SECONDS` (default 30s). Read on every agent's SessionStart.

### `GET /signal` — current amygdala alert

```bash
curl -s "$BRAIN_URL/signal" -H "Authorization: Bearer $KB_ROUTER_TOKEN"
```

Absent `amygdala-active.md` → `{ "active": false, ... }`. Dedup via `hash`.

### `POST /marker` — emit a brain marker

Four marker types, four destinations:

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
  -d '{"type":"lesson","content":"NFS dirs need 777 for UID 1000 writers","attrs":{"source":"deploy","session_id":"abc"}}'
```

Idempotency-key scoped to 1 h (TTL configurable). Replay returns the original response with `idempotent_replay: true`.

### `POST /tick` — request a manual brain tick

```bash
curl -s -X POST "$BRAIN_URL/tick?dry_run=true" -H "Authorization: Bearer $KB_ROUTER_TOKEN"
```

File-protocol: writes a request file to `brain-feed/ticks/requested/`. The tick-engine CronJob (or a local daemon) consumes it and moves it to `completed/` or `failed/`. Poll `GET /tick/{job_id}` for status.

### `POST /ingest` — universal ingest

```bash
curl -s -X POST "$BRAIN_URL/ingest" \
  -H "Authorization: Bearer $KB_ROUTER_TOKEN" \
  --data-urlencode "message=Check out https://example.com/paper.pdf — related to current work"
```

Classifies via Haiku, fans out URLs/repos/files to artifact-store, writes a markdown note to `raw/inbox/`. See [`api/openapi.yaml`](api/openapi.yaml) for the full spec.

---

## Vault schema

The vault is an Obsidian-compatible folder tree, writable by humans and by kernel services. `brain scaffold` is the only authoritative writer of the schema marker (`.brain-schema`).

```
<vault>/
  .brain-schema           # version marker (JSON)
  README.md  CLAUDE.md    # vault rules for AI agents

  # Cognitive regions (owned by tick-engine + daemons)
  raw/                    # ingest staging
    {inbox,articles,media,transcripts}/
  clusters/               # canonical arc storage — tick-engine writes here
  brain-feed/             # hot feed — /feed reads here, /tick writes ticks/requested/
  amygdala/               # emergency signals — /marker type=signal lands here
  frontal-lobe/           # working memory
    {conscious,unconscious}/
  pineal/                 # joy + breakthrough region

  # Knowledge base (operator owns — agents curate)
  identity/               # who you are — root node
    about-me.template.md  goals.template.md  principles.template.md  stack.template.md
  left/                   # technical + systematic hemisphere
    _index.md  projects/  research/  reference/  decisions/  incidents/
  right/                  # creative + strategic hemisphere
    _index.md  ideas/  strategy/  life/  creative/  risk/
  bridge/                 # cross-hemisphere synthesis
    _index.md  vision.md  connections.md  weekly-synthesis.md
  daily/                  # append-only daily logs

  templates/              # starters for new notes + full MUBS folder template
    mubs/                 # VISION SPECS BLOCKS TODO STATE BUGS KNOWN-ISSUES ENHANCEMENTS MVP PATCHES
```

Scaffold is idempotent: re-run any time, it never overwrites your edits. Schema-version mismatch is a hard error unless `--force-upgrade` is passed.

Full detail: [`docs/VAULT-SCHEMA.md`](docs/VAULT-SCHEMA.md).

---

## Configuration

All config lives in `~/.agentibrain/`:
- `config.yaml` — vault path, mode (`local` / `s3`), endpoints.
- `.env` — secrets (chmod 600). Tokens, API keys.
- `compose.yml` — rendered from templates; re-rendered by `brain init`.

Environment variables every service understands:

| Env var | Default | Purpose |
|---|---|---|
| `VAULT_ROOT` | `/vault` | Vault mount path |
| `KB_ROUTER_TOKENS` / `KB_ROUTER_TOKEN` | — | Bearer auth (comma-sep list or single) |
| `OBSIDIAN_READER_URL` | `http://obsidian-reader:8080` | Reader service URL |
| `EMBEDDINGS_URL` | `http://embeddings:8080` | Embeddings service URL |
| `INFERENCE_URL` | — | LLM proxy URL (optional — falls back to regex classifier) |
| `FEED_CACHE_TTL_SECONDS` | `30` | `/feed` cache window |
| `IDEMPOTENCY_TTL_SECONDS` | `3600` | `/marker` replay window |

---

## Development

```bash
git clone https://github.com/The-Cloud-Clock-Work/agentibrain-kernel
cd agentibrain-kernel
python -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'

# Unit tests (scaffold, compose render, bootstrap)
pytest tests/unit

# kb-router service tests (25 cases — feed, signal, marker, tick)
pip install -r services/kb-router/requirements.txt
PYTHONPATH=services/kb-router:. pytest services/kb-router/tests -q

# Build a service image locally
docker build -t agentibrain-kb-router:local services/kb-router
```

Branching: `dev` is the working branch. PRs go `dev` → `main`. `main` is what ships (`:latest` images + PyPI releases on tag push).

---

## Status

**v0.1.0 — first stable.** Dev + prod deploys live in `anton-{dev,prod}` (5 agentibrain-* pods per env, Phase 7 HTTP contract shipped, brain-blind boundary verified end-to-end 2026-04-27). External-user install story + further hardening tracked in [`operator/`](operator/):

- [`operator/VISION.md`](operator/VISION.md) — what 100% means
- [`operator/STATE.md`](operator/STATE.md) — current maturity snapshot
- [`operator/BLOCKS.md`](operator/BLOCKS.md) — in-flight work (Tier 1+2)
- [`operator/ENHANCEMENTS.md`](operator/ENHANCEMENTS.md) — backlog (Tier 3-5)
- [`operator/TODO.md`](operator/TODO.md) — next actions with ready-to-run commands

The kernel is the **canonical and exclusive** source of truth for:
- 4 brain services (kb-router, obsidian-reader, embeddings, tick-engine)
- Helm charts (`helm/agentibrain-{kb-router,obsidian-reader,embeddings,brain-keeper,brain-cron}/`)
- brain-keeper agent definition (`agents/brain-keeper/`)
- brain + brain-keeper agentihooks profiles (`profiles/{brain,brain-keeper}/`)
- Brain HTTP contract (`/feed /signal /marker /tick /index_artifact /ingest`) — see [`docs/API.md`](docs/API.md)

Downstream repos (`agentihub`, `agentihooks-bundle`, `antoncore`) are **brain-blind** as of 2026-04-27 — no vendored copies, no kernel-sync scripts. They consume kernel artifacts at deploy time only:
- `antoncore/k8s/charts/agentibrain-*/` — Helm values overlaying kernel charts
- `antoncore/k8s/argocd/{dev,prod}/agentibrain-*.yaml` — ArgoCD apps pinned to kernel images (`ghcr.io/the-cloud-clock-work/agentibrain-*:{dev,latest}`)
- Agent pods reach the kernel via `BRAIN_URL=http://agentibrain-kb-router.anton-{dev,prod}.svc:8080`

---

## Further reading

- [`docs/architecture/ARCHITECTURE.md`](docs/architecture/ARCHITECTURE.md) — full kernel design
- [`docs/architecture/CLUSTERS.md`](docs/architecture/CLUSTERS.md) — arc lifecycle (write, heat, graduate)
- [`docs/architecture/KEEPER.md`](docs/architecture/KEEPER.md) — brain-keeper agent
- [`docs/architecture/MARKERS.md`](docs/architecture/MARKERS.md) — marker grammar (`<!-- @lesson --> … <!-- @/lesson -->`)
- [`docs/architecture/SYMBIOSIS.md`](docs/architecture/SYMBIOSIS.md) — how the kernel relates to agenticore + agentihooks
- [`docs/architecture/TELEMETRY.md`](docs/architecture/TELEMETRY.md) — OTel spans + Langfuse integration
- [`docs/architecture/MATURITY.md`](docs/architecture/MATURITY.md) — kernel maturity rubric
- [`docs/VAULT-SCHEMA.md`](docs/VAULT-SCHEMA.md) — vault layout v1
- [`api/openapi.yaml`](api/openapi.yaml) — HTTP contract

---

## License

[MIT](LICENSE).
