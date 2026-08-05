# Running agentibrain-kernel locally (Docker Compose)

The kernel ships as a Helm-friendly k8s deploy and as a docker-compose
deploy for laptops. This directory holds the compose entry point.

## Quickstart

```bash
git clone https://github.com/The-Cloud-Clockwork/agentibrain-kernel.git
cd agentibrain-kernel
./local/bootstrap.sh              # writes .env + scaffolds ~/agentibrain-vault
docker compose up -d              # 8 containers come up
docker compose ps                 # see the health note below
```

Run `bootstrap.sh` as your normal user — **never with sudo**. Under sudo,
`$HOME` is `/root`, so the vault and `.env` land in root's home and the stack
breaks for your user (the script now refuses to run as root for this reason).
A `Permission denied` from bootstrap means root-owned files from an old
container run: `sudo chown -R $(id -u):$(id -g) <path>` and re-run plain.

`postgres`, `redis`, `brain-api`, `embeddings`, and `mcp` report `(healthy)`.
`tick-cron`, `tick-drain`, and `amygdala` show a bare `Up` — they are batch
workers with no healthcheck, so the absence of `(healthy)` on those three is
expected, not a fault. Judge them by their logs instead.

On macOS, enable VirtioFS (Docker Desktop → Settings → General → "VirtioFS")
for fast bind mounts. If you ever see root-owned files under the vault
(`~/agentibrain-vault` by default) from a prior run,
`sudo chown -R $(id -u):$(id -g) ~/agentibrain-vault`.

Smoke the brain feed:

```bash
TOK=$(grep ^KB_ROUTER_TOKEN .env | cut -d= -f2)
curl -H "Authorization: Bearer $TOK" http://localhost:8103/feed | jq .
```

You should see JSON with `hot_arcs`, `inject_blocks`, and `entries`. On a
fresh vault these arrays start mostly empty — they fill as the tick runs
and as you write markers.

Tear down:

```bash
docker compose down            # keep volumes (preserve vault, postgres, redis)
docker compose down -v         # nuke volumes too (full reset)
```

## Updating to a newer version

Compose **builds from the source tree in this repo** — every service declares a
`build:` context, so the running containers are whatever your working copy said
at build time. Pulling new code does not change a running container, and
`docker compose up -d` alone will not rebuild it: compose sees an image with the
expected tag already present and reuses it.

The update is therefore this sequence, and `--build` is the step people skip:

```bash
git pull
./local/bootstrap.sh              # idempotent: re-pins .env, migrates the vault if needed
docker compose up -d --build      # rebuild changed services, recreate them
docker compose ps                 # the five healthchecked services report (healthy)
```

Re-running `bootstrap.sh` on every update is safe and is what makes an update
seamless across layout changes: it keeps your tokens, upgrades a legacy
in-repo `./vault` to `~/agentibrain-vault` (moving the data), and pins the
resolved path in `~/.agentibrain/.env`. When the vault path changes, compose
sees a changed bind mount and recreates the affected containers on the next
`up -d` — no manual `down` required.

`--build` is what makes the new code take effect. If you prefer the explicit
form:

```bash
git pull
./local/bootstrap.sh              # re-pin .env + migrate vault if needed
docker compose build              # rebuild images from the new source
docker compose down               # stop old containers (volumes survive)
docker compose up -d              # start on the new images
```

Both are equivalent. `--build` is shorter and only recreates services whose
image actually changed.

### Which branch to pull

| Branch | What it is | Use it when |
|---|---|---|
| `dev` | Working branch. Every push publishes `:dev` images. Newest fixes land here first. | You want current behaviour, and can tolerate the occasional rough edge. |
| `main` | Snapshot branch. Updated only by a reviewed `dev` → `main` PR; nothing deploys from it. | You want a checkpoint that someone deliberately stamped as known-good. |

```bash
git checkout dev && git pull      # newest
git checkout main && git pull     # last stamped snapshot
```

### Updating only one service

Six services declare a build, but they share only **four** images —
`tick-cron`, `tick-drain`, and `amygdala` are all the same `brain-ops` image
with different entrypoints. Rebuilding all four takes a few minutes. If you
know what changed:

```bash
docker compose up -d --build mcp          # e.g. only services/mcp/ changed
docker compose up -d --build brain-api embeddings
```

Map of source directory → compose service:

| You changed | Rebuild |
|---|---|
| `services/brain-api/` | `brain-api` |
| `services/embeddings/` | `embeddings` |
| `services/mcp/` | `mcp` |
| `services/brain-ops/` | `tick-cron` `tick-drain` `amygdala` (all three share the image) |

### Verify the update actually landed

Rebuilding silently reusing a cached layer is the failure mode worth checking
for. Confirm the container is younger than your `git pull`:

```bash
docker compose ps --format 'table {{.Service}}\t{{.Image}}\t{{.RunningFor}}'
docker compose logs --since 2m brain-api | head
```

If a service still reports an old age, it was not recreated — re-run with
`--force-recreate`.

### What survives an update

Named volumes and the vault bind-mount are untouched by `down` / `up --build`:
your arcs, embeddings, and Redis state all persist. Only `docker compose down -v`
destroys them. A schema change that needs a fresh database says so in
[`CHANGELOG.md`](../CHANGELOG.md); there is no automatic migration step.

## Architecture (local mode)

```
                    ┌──────────────────────────────┐
                    │ your tools / agents / curl   │
                    └──────────────┬───────────────┘
                                   │  HTTP + Bearer
       ┌───────────────────────────┴───────────────────────────┐
       │                                                         │
   ┌───▼─────────┐                  ┌─────────────────┐   ┌───▼───┐
   │ brain-api   │                  │ embeddings      │   │ mcp   │
   │ :8103       │                  │ :8102           │   │ :8104 │
   └─┬─────┬─────┘                  └────────┬────────┘   └───────┘
     │     │                                 │
     │     │         ┌──────────────┐        ▼
     │     │         │ vault (RW)   │ ┌──────────────┐
     │     ├────────▶│ ~/agentibrain│ │ postgres+    │
     │     │         │ -vault by    │ │              │
     │     │         │ default      │ │ pgvector     │
     │     │         └──────────────┘ └──────────────┘
     │     │                ▲
     │  tick-cron ──────────┤  (every TICK_INTERVAL_SECONDS — default 2h)
     │  tick-drain ─────────┤  (every TICK_DRAIN_INTERVAL_SECONDS — default 30s)
     │  amygdala  ──────────┤  (continuous, polls Redis stream)
     │                      │
     ▼                      │
  redis (DB 11) ────────────┘
```

8 containers: 3 service-layer (brain-api, embeddings, mcp)
+ 3 brain-ops workers (tick-cron, tick-drain, amygdala) + postgres + redis.
All three brain-ops workers run the same image with different entrypoints.

## Inference modes

The brain has two phases per tick: **deterministic** (always runs) and **AI**
(optional, requires `INFERENCE_URL`). Without inference, you still get hot
arcs, signals, decay, marker writes, and broadcasts — only the AI summary
phase is skipped.

| Mode | Setup | Notes |
|---|---|---|
| **No AI** (default) | leave `INFERENCE_URL=` empty in `.env` | Lowest cost. Brain runs deterministic-only. Embeddings disabled too if `LLM_API_KEY` is empty. |
| **Ollama overlay** | `docker compose -f compose.yml -f local/compose.ollama.yml up -d` then `docker compose exec ollama ollama pull llama3.2` | Adds an Ollama container, pre-wires `INFERENCE_URL=http://ollama:11434/v1`. No API key needed. Recommended models below. |
| **OpenAI direct** | set `LLM_API_KEY=sk-...` (also embeds), `INFERENCE_URL=https://api.openai.com/v1` | Highest quality embeddings. Each tick costs cents. |
| **Anthropic via LiteLLM** | run a LiteLLM proxy, set `INFERENCE_URL=http://your-litellm/v1` and `LLM_API_KEY=` to your LiteLLM virtual key | Most flexible — single key fans out to multiple providers. |
| **Other OpenAI-compatible** | LM Studio, vLLM, llama-server, etc. | Anything that speaks `/v1/chat/completions` works. |

### Recommended Ollama starter models

| Host RAM | Model | Why |
|---|---|---|
| 8 GB | `llama3.2:3b` | Fast, decent reasoning for ticks. |
| 16 GB | `llama3.1:8b` | Balanced quality. |
| 32 GB+ | `qwen2.5:14b` or `mistral-nemo:12b` | Sharper synthesis. |

```bash
docker compose exec ollama ollama pull llama3.2:3b
```

The kernel sends a `model` field on each request — set it in your tick
config or use Ollama's default-model behavior. The `route` field that the
kernel sends for inference-gateway routing is silently ignored by Ollama.

## Vault layout

```
vault/
├── README.md           # written by bootstrap.sh
├── raw/inbox/          # incoming markers (one .md per /marker call)
├── brain-feed/         # generated by ticks (hot-arcs.md, signals.md, last-tick.md, …)
└── clusters/           # arc cluster files (one per active arc)
```

By default the vault lives at `~/agentibrain-vault` (in $HOME, outside the
repo checkout, bind-mounted into containers at `/vault`). Earlier versions
scaffolded `./vault` inside the repo — `bootstrap.sh` migrates that
automatically when the new location doesn't exist yet. To use your existing
Obsidian vault instead:

```bash
echo 'VAULT_ROOT_HOST=/Users/you/Documents/MyVault' >> .env
docker compose up -d
```

Path can be absolute or relative.

## Seeding the vault from Claude Code transcripts

`tick-cron` clusters arcs out of Claude Code session transcripts. The host's
`~/.claude/projects` is mounted read-only at `/shared/.claude/projects`
(override the host side with `CLAUDE_PROJECTS_HOST` in `.env`). Extraction runs
once daily at `EXTRACT_HOUR` UTC (default `04`), looking back `EXTRACT_SINCE`
(default `26h`) and skipping sessions shorter than `EXTRACT_MIN_TURNS` turns.
Single-digit hours are accepted (`EXTRACT_HOUR=4` ≡ `04`). A mounted-but-empty
transcripts directory is treated as "not mounted" and extraction is skipped —
Docker silently creates a missing host directory, so emptiness is the only
reliable signal that `CLAUDE_PROJECTS_HOST` points at the wrong place.

A fresh vault starts empty — nothing appears until the first extraction window.
To seed it immediately from historical transcripts:

```bash
EXTRACT_ON_BOOT=1 docker compose up -d --force-recreate tick-cron
docker compose logs -f tick-cron   # watch the "[tick-cron] extraction:" pass
```

`EXTRACT_BOOT_SINCE` (default `90d`) bounds the backfill window. Drop
`EXTRACT_ON_BOOT` afterwards — leaving it set re-runs the seed on every
container start (idempotent but wasteful).

Markers (`@lesson`, `@signal`, `@decision`, `@milestone`) arrive separately:
agentihooks POSTs them to brain-api when `BRAIN_URL` is set in the agent's
environment (e.g. `http://127.0.0.1:8103` for this compose stack, plus
`KB_ROUTER_TOKEN` for auth). Without `BRAIN_URL`, markers never reach the
vault.


## Common operations

```bash
# Watch a service log
docker compose logs -f brain-api

# Run an immediate tick (don't wait the 2 hours)
# Queues a request; tick-drain picks it up within TICK_DRAIN_INTERVAL_SECONDS
# and ALSO refreshes the semantic index, so the content becomes searchable.
TOK=$(grep ^KB_ROUTER_TOKEN .env | cut -d= -f2)
curl -X POST -H "Authorization: Bearer $TOK" \
  "http://localhost:8103/tick?no_ai=false&source=manual"

# Watch it land
docker compose logs -f tick-drain

# Write a marker by hand
TOK=$(grep ^KB_ROUTER_TOKEN .env | cut -d= -f2)
curl -X POST -H "Authorization: Bearer $TOK" \
  -H "Content-Type: application/json" \
  -H "X-Idempotency-Key: $(uuidgen)" \
  -d '{"type":"lesson","title":"Title here","body":"Body markdown."}' \
  http://localhost:8103/marker

# Pull current signals (amygdala)
curl -H "Authorization: Bearer $TOK" http://localhost:8103/signal | jq .

# Reset everything (dangerous — wipes postgres, redis, vault stays)
docker compose down -v
./local/bootstrap.sh && docker compose up -d
```

## Troubleshooting

**Postgres not ready / embeddings keeps restarting**
- `docker compose logs postgres | grep ERROR`
- The pgvector extension is installed via `local/sql/00-init.sql` on first
  boot only. If you mounted an existing pgdata volume that lacks pgvector,
  run: `docker compose exec postgres psql -U brain -d brain -c "CREATE EXTENSION IF NOT EXISTS vector;"`.

**Vault permission errors (root-owned files inside container)**
- The services run as non-root. If a previous run as root left files behind,
  `sudo chown -R $(id -u):$(id -g) ~/agentibrain-vault`.
- On macOS / Windows: enable VirtioFS / WSL2 native filesystem for fast bind
  mounts.

**Port collisions**
- 5432, 6379, 8102–8104 default. Override in `.env`:
  ```
  PORT_BRAIN_API=18103
  PORT_POSTGRES=15432
  ```

**`401 Unauthorized` when curling `/feed`**
- Token mismatch. Run `grep ^KB_ROUTER_TOKEN .env` and use that exact value.
  Don't paste the literal `__GENERATE__` placeholder.

**`/feed` returns empty hot_arcs / no inject_blocks**
- Expected on a fresh vault. Write a few markers, wait for a tick (or run one
  manually with the snippet above), then re-check.

**AI tick logs "INFERENCE_URL not set; skipping AI phase"**
- That's a feature, not a bug. Either set `INFERENCE_URL` or use the Ollama
  overlay.

**Ollama OOMs / takes minutes per tick**
- Pick a smaller model from the table above. `ollama list` to see what's
  pulled. `ollama rm <model>` to free disk.

**`docker compose up` is slow on first run**
- Each service builds its own image (~5 min total on a fast machine). After
  the first run the layers are cached.

## Pre-built images

The compose builds images locally (`:local` tag). To skip building and pull
published images instead, replace each `build:` block in `compose.yml` with:

```yaml
image: ghcr.io/the-cloud-clockwork/agentibrain-<service>:dev
```

Available services: `brain-api`, `embeddings`, `brain-ops`, `mcp`.

**`:dev` is the only published tag.** CI builds on every push to `dev`; nothing
publishes `:latest` or a `main`-tracking tag, so a config naming one will fail
to pull. Updating a pulled deployment is `docker compose pull && docker compose
up -d` — no `--build`, because there is nothing local to build.

## What's NOT in local mode

- `brain-keeper` — the agenticore-based ops oracle. Heavy dep tree (Claude
  OAuth + GitHub PAT + LiteLLM). Future `compose.keeper.yml` overlay.
- ArgoCD / Helm — that's the production path; see `helm/README.md`.
- HTTPS / public-internet exposure — local-only on `localhost`. If you want
  to expose the brain externally, a Traefik / Caddy bolt-on is documented
  separately.
