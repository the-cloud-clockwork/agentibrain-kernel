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
| `agentibrain init` | Render a self-contained stack into `~/.agentibrain/` (config, `.env` with fresh tokens, compose file). For machines NOT using the repo's root compose. |
| `agentibrain build [SERVICE...]` | **Rebuild + restart** — `docker compose up -d --build` in the detected deployment, then `ps`. The one command that makes a code change take effect. |
| `agentibrain up` / `down` | Start / stop the detected deployment (init mode also runs migrations on `up`). |
| `agentibrain logs [SERVICE] [-f] [--since 10m] [--tail N]` | Service logs passthrough. |
| `agentibrain status` | `docker compose ps` (init stacks) + shallow `GET /health`. |
| `agentibrain check` | **Deep verification** — see below. |
| `agentibrain tick [--dry-run] [--no-ai] [--wait]` | Enqueue a brain tick; `--wait` blocks until it completes. |
| `agentibrain sync [--wait\|--check]` | **Re-ingest everything** — replay buffered markers (`~/.agentihooks/brain-outbox` + `-backlog`) into `POST /marker`, then enqueue a tick so replays cluster and the `raw/` index refreshes. Idempotent; original timestamps preserved. `--wait` blocks until the tick completes; `--check` does the same but narrates: per-buffer progress counters, tick state changes, and a final summary with remaining buffered files. Exit: 0 clean, 1 hard failure, 2 degraded. |
| `agentibrain scaffold [PATH]` | Write/repair the vault layout schema. Authoritative writer of `.brain-schema`. |
| `agentibrain version` | Print version. |

## Two deployment modes

Every stack command (`build`/`up`/`down`/`logs`/`status`) **auto-detects**
where the deployment lives, from any cwd:

1. The checkout you are standing in — a `compose.yml` found walking up from
   the current directory always wins, so working in checkout B never targets
   a checkout A pinned by an older bootstrap
2. The repo path `local/bootstrap.sh` pinned as `AGENTIBRAIN_REPO` in
   `~/.agentibrain/.env` (covers every other cwd)
3. The init-rendered stack in `~/.agentibrain/` (`agentibrain init` mode)

No deployment anywhere → exit 2 with the bootstrap/init hint. You never need
to remember where the compose file is or type `docker compose` yourself.

## Testing a running brain

```bash
agentibrain check          # exit 0 = healthy, 1 = degraded
```

No URL needed locally — the CLI targets `http://localhost:8103` (brain-api's
published port) by default; override the port with `PORT_BRAIN_API` in
`~/.agentibrain/.env` or point at a remote brain with `--brain-url` /
`$BRAIN_URL`.

`check` calls `GET /health/deep`, which round-trips a real vault write, makes
the embeddings service hit Postgres and run an actual embedding call
(validating the model dimension against the pgvector schema), and verifies the
inference gateway accepts the configured key. Token resolves from
`$KB_ROUTER_TOKEN` or `~/.agentibrain/.env` automatically; override with
`--token`.

Shallow variant:

```bash
agentibrain status
```

Raw equivalents (no install needed):

```bash
TOK=$(grep ^KB_ROUTER_TOKEN .env | cut -d= -f2)
curl -H "Authorization: Bearer $TOK" http://127.0.0.1:8103/health/deep | jq .
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
