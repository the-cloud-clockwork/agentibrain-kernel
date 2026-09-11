# `agentibrain` CLI

Command-line client for the kernel. Not on PyPI — install from the checkout:

```bash
cd agentibrain-kernel
pip install -e .
agentibrain --version
```

> Renamed from `brain` in v0.9 — hard rename, no alias. If `brain` still works
> in your shell, reinstall (`pip install -e . --force-reinstall --no-deps`).

## Commands

| Command | What it does |
|---|---|
| `agentibrain build [SERVICE...]` | **Rebuild + restart** — `docker compose up -d --build` in the detected deployment, then `ps`. The one command that makes a code change take effect. |
| `agentibrain up` / `down` | Start / stop the detected deployment (the `~/.agentibrain` stack also runs migrations on `up`). |
| `agentibrain logs [SERVICE] [-f] [--since 10m] [--tail N]` | Service logs passthrough. |
| `agentibrain status` | `docker compose ps` of the detected deployment + shallow `GET /health`. |
| `agentibrain check` | **Deep verification** — see below. |
| `agentibrain tick [--dry-run] [--no-ai] [--wait]` | Enqueue a brain tick; `--wait` blocks until it completes. |
| `agentibrain sync [--wait\|--check]` | **Re-ingest everything** — replay buffered markers (`~/.agentihooks/brain-outbox` + `-backlog`) into `POST /marker`, then enqueue a tick so replays cluster and the `raw/` index refreshes. Idempotent; original timestamps preserved. `--wait` blocks until the tick completes; `--check` does the same but narrates: per-buffer progress counters, tick state changes, and a final summary with remaining buffered files. Exit: 0 clean, 1 hard failure, 2 degraded. |
| `agentibrain install` | **Whole-machine setup, idempotent.** Five steps: reuse or render a stack — bundled MinIO unless `--s3-bucket` (with `--s3-endpoint`) names S3, bundled Ollama with `--ollama` (export `BRAIN_OLLAMA_CHAT_MODEL` first to size the model; it is fixed at creation, and `--ollama`, `--s3-*`, `--postgres-url` and `--redis-url` against an existing stack do nothing), scaffold the vault, complete the brain's own `~/.agentibrain/.env` and create the marker outbox, start the stack, then link the packaged brain profile. Completion is additive and runs before the stack starts: every key the file lacks is added — the bearer and the embeddings key pair generated, the stack's settings at the value the deployment's compose file already uses (else the kernel default), agentihooks' brain settings (`BRAIN_ENABLED`, `BRAIN_SOURCE_PATH`, `AMYGDALA_ENABLED`, `AMYGDALA_SIGNAL_PATH`, `BRAIN_WRITER_ENABLED`, `BRAIN_WRITER_MAX_MARKERS`, `BRAIN_WRITER_OUTBOX`) at their defaults — while a key already present, even with an empty value, is never rewritten, removed or backed up (chmod 600). `LLM_API_KEY`, `LLM_API_BASE`, `INFERENCE_URL` and `INFERENCE_API_KEY` are written only when the compose file supplies them (a bundled-Ollama stack); otherwise install names the ones left for you to set. agentihooks reads that file directly, so nothing is copied and any earlier projected copy is swept. Step three is the one nobody does by hand: without `BRAIN_URL` beside the bearer the brain's file is only half the answer, which is how a machine ends up authenticating every `agentibrain` command while every marker POST answers 401. `--brain-url` (envvar `BRAIN_URL`) makes it **client-only** — wire this machine to a brain that runs elsewhere, skipping the stack and the vault, since inference and storage belong to that stack; pair it with `--token` (envvar `KB_ROUTER_TOKEN`), matching `check` / `tick` / `sync`. Flags: `--brain-url`, `--token`, `--vault`, `--ollama`, `--s3-bucket`, `--s3-endpoint`, `--postgres-url`, `--redis-url`, `--openai-key` and `--llm-gateway-url` (these two fill `LLM_API_KEY`/`INFERENCE_API_KEY` and `INFERENCE_URL` only when the file lacks them), `--name`, `--profile`, `--for-target`, `--no-stack`, `--no-link`, `--no-init`, `--dry-run`. |
| `agentibrain scaffold [PATH]` | Write/repair the vault layout schema. Authoritative writer of `.brain-schema`. |
| `agentibrain version` | Print version. |

## Two deployment modes

Every stack command (`build`/`up`/`down`/`logs`/`status`) **auto-detects**
where the deployment lives, from any cwd:

1. The checkout you are standing in — a `compose.yml` found walking up from
   the current directory always wins, so working in checkout B never targets
   a checkout A pinned by an older bootstrap
2. The stack Docker reports holding `agentibrain_brain_api` — whatever is up
   is what gets driven
3. The repo path pinned as `AGENTIBRAIN_REPO` in `~/.agentibrain/.env` —
   written by `local/bootstrap.sh`, `install`, or the first stack command that
   drives a checkout when the file lacks it (never rewritten), so `up` after
   `down` returns to that checkout from any cwd
4. The stack `install` rendered into `~/.agentibrain/`

No deployment anywhere → exit 2 with the bootstrap/init hint. You never need
to remember where the compose file is or type `docker compose` yourself.

**One stack per machine.** Both compose files hardcode the `agentibrain_*`
container names, so `up`, `build` and `install` first `docker compose down`
every other compose project holding one, and `down` removes all of them.
Volumes survive.

## Network exposure and auth

Set by the compose template and the root `compose.yml`; no flag needed.

| Service | Published on | Why |
|---|---|---|
| `postgres`, `redis`, `minio`, `embeddings`, `ollama` | `127.0.0.1` | Reached over the compose network. Nothing outside the machine needs them, and some carry generated default credentials. |
| `brain-api` (8103), `mcp` (8104) | `${BIND_HOST:-0.0.0.0}` | The two a client-only install has to reach. Set `BIND_HOST=127.0.0.1` to keep them local and front them with a proxy. |

A bare `HOST:CONTAINER` mapping binds every interface, and Docker's DNAT rules
sit ahead of a host firewall — which is why the datastores are pinned rather
than left to a default.

**brain-api fails closed.** Without `KB_ROUTER_TOKEN` (or `KB_ROUTER_TOKENS`)
every endpoint answers `503`, including `/health/deep` and `/feed`.
`install` always generates a bearer, so an empty set is a misconfiguration, not
a decision to be public. `agentibrain check` surfaces it as a hard failure.

## Testing a running brain

```bash
agentibrain check          # exit 0 = clean, 1 = broken, 2 = degraded
```

No URL needed locally — the CLI targets `http://localhost:8103` (brain-api's
published port) by default; override the port with `PORT_BRAIN_API` in
`~/.agentibrain/.env` or point at a remote brain with `--brain-url` /
`$BRAIN_URL`. Token resolves from `$KB_ROUTER_TOKEN` or `~/.agentibrain/.env`
automatically; override with `--token`.

`check` asks two questions, because a stack can pass one and fail the other.

**Do the dependencies work?** (`GET /health/deep`) — round-trips a real vault
write, makes the embeddings service hit Postgres and run an actual embedding
call (validating the model dimension against the pgvector schema), and
verifies the inference gateway accepts the configured key with a real
one-token completion.

**Is the loop actually flowing?** (`GET /health/pipeline`) — seven stages, in
the order data travels, each reporting the evidence behind its verdict:

| Stage | Fails when |
|---|---|
| `ingest` | — (warns when no marker has been written for `PIPELINE_MARKER_QUIET_HOURS`) |
| `drain` | requests pile up unconsumed, or the most recent tick failed — the `error_tail` is quoted inline |
| `arcs` | arcs exist but `hot-arcs.md` was never written |
| `lessons` | a lesson log sits outside `left/reference/`, the feed is missing, or recent lessons produce an empty feed |
| `signals` | signal files exist but `signals.md` was never written (warns when the file is stale, so its TTL sweep has not run) |
| `feed` | `brain-feed/` holds files but none parse as feed entries — nothing reaches a session |
| `index` | source files exist for a producer the index holds no rows for |

A stage with no input reports `ok`. A fresh vault has no arcs, no lessons and
no ticks; calling that broken would make the command worth ignoring.

Both run server-side, where the vault is mounted — so the same report is
available for a remote deployment, not just the machine you are sitting at.
The one check the server cannot make is added locally: markers still buffered
in this machine's agentihooks outbox, which look like silence from the far
side. `agentibrain sync` replays them.

```bash
agentibrain check --pipeline-only   # skip the LLM/embedding round-trips
agentibrain check --deps-only       # just the dependency probes
agentibrain check --json            # both payloads, machine-readable
```

Shallow variant:

```bash
agentibrain status
```

Raw equivalents (no install needed):

```bash
TOK=$(grep ^KB_ROUTER_TOKEN .env | cut -d= -f2)
curl -H "Authorization: Bearer $TOK" http://127.0.0.1:8103/health/deep | jq .
curl -H "Authorization: Bearer $TOK" http://127.0.0.1:8103/health/pipeline | jq '.stages'
docker compose logs tick-cron | grep extraction     # did extraction run
curl -H "Authorization: Bearer $TOK" http://127.0.0.1:8103/feed | jq '.hot_arcs'
```

## Forcing work on demand

```bash
agentibrain tick --no-ai --wait     # deterministic tick, blocks until done
agentibrain tick --wait             # full AI tick
agentibrain tick --dry-run --wait   # read-only verify, no writes
agentibrain sync --check            # replay marker buffers + reingest raw/, narrated
```

No host crons, ever: when the stack runs under compose, `tick-cron` drains
the same marker buffers and refreshes the `raw/` index automatically every
`TICK_INTERVAL_SECONDS` (default 2 h). `sync` is the on-demand version of
what the stack already does on its own.

Transcript extraction (arcs from Claude Code sessions) is a compose concern,
not a CLI one: `EXTRACT_ON_BOOT=1 docker compose up -d --build` — see
[`../local/README.md`](../local/README.md) "Seeding the vault".
