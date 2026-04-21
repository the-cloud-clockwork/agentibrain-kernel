# Install — AWS path

Use this when you want brain artifacts to live in AWS S3.

## Prerequisites

- Python 3.10+
- Docker + Docker Compose v2 plugin
- An S3 bucket and AWS credentials exposed in env (the embeddings service reads
  `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` / `AWS_REGION` if present)
- (optional) `psql` client for migration runs
- An OpenAI API key

## Install

```bash
pip install agentibrain

brain init \
  --vault ~/agentibrain-vault \
  --s3-bucket my-brain-bucket \
  --openai-key $OPENAI_API_KEY

brain up        # docker compose up -d on the rendered compose
brain scaffold  # seed the vault folder layout + .brain-schema
brain status    # compose ps + GET /health against kb-router
```

After `brain init`, the KB router token is printed to stdout and persisted to
`~/.agentibrain/.env` with mode `0600`. Use that token in the
`Authorization: Bearer <token>` header when hitting the kernel HTTP API.

## What gets written

| Path | Purpose |
|---|---|
| `~/.agentibrain/config.yaml` | Non-secret settings snapshot. |
| `~/.agentibrain/.env` (0600) | `KB_ROUTER_TOKEN`, `OPENAI_API_KEY`, optional `INFERENCE_URL`. |
| `~/.agentibrain/compose.yml` | Rendered docker-compose for the stack (no MinIO in S3 mode). |

## Overriding ports

`brain init` accepts `--postgres-url` and `--redis-url` to point at externally-
managed databases; otherwise bundled Postgres/Redis listen on 5432 and 6379 on
localhost.

## Upgrades

- `pip install -U agentibrain` pulls a new CLI.
- `brain init` is safe to re-run; it overwrites `compose.yml` but keeps `config.yaml` consistent.
- New kernel service images flow via `docker compose pull` + `brain up`.

## Uninstall

```bash
brain down
rm -rf ~/.agentibrain
# vault files are yours — they stay on disk until you delete them.
```
