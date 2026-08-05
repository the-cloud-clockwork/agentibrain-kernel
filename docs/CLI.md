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
| `agentibrain up` / `down` | Start / stop the init-rendered stack. Exits 2 with a hint if you never ran `init`. |
| `agentibrain status` | `docker compose ps` (init stacks) + shallow `GET /health`. |
| `agentibrain check` | **Deep verification** — see below. |
| `agentibrain tick [--dry-run] [--no-ai] [--wait]` | Enqueue a brain tick; `--wait` blocks until it completes. |
| `agentibrain scaffold [PATH]` | Write/repair the vault layout schema. Authoritative writer of `.brain-schema`. |
| `agentibrain version` | Print version. |

## Two deployment modes

| Mode | Stack lives in | Start/stop with | `up`/`down` work? |
|---|---|---|---|
| **Root compose** (this repo, `./local/bootstrap.sh`) | repo `compose.yml` | `docker compose` in the repo | No — use `docker compose`. `status`/`check`/`tick` work fine. |
| **Init-rendered** (`agentibrain init`) | `~/.agentibrain/compose.yml` | `agentibrain up` / `down` | Yes |

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
```

Transcript extraction (arcs from Claude Code sessions) is a compose concern,
not a CLI one: `EXTRACT_ON_BOOT=1 docker compose up -d --build` — see
[`../local/README.md`](../local/README.md) "Seeding the vault".
