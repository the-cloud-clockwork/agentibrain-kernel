# Install — Local / MinIO path (no AWS required)

Use this for self-hosting without an AWS account. MinIO provides an S3-
compatible store inside the same compose stack.

## Prerequisites

- Python 3.10+
- Docker + Docker Compose v2 plugin
- (optional) `psql` client
- An OpenAI API key

## Install

```bash
pip install agentibrain

brain init --local --vault ~/agentibrain-vault --openai-key $OPENAI_API_KEY
brain up
brain scaffold
brain status
```

## What `--local` changes

- Adds a `minio` service (S3-compatible) on ports 9000/9001.
- Adds a `minio-init` one-shot that creates the default bucket (`agentibrain-artifacts`).
- Sets `ARTIFACT_STORE_URL` default to point at MinIO-backed storage when the
  operator's artifact-store is not supplied.
- Keeps the vault on the host filesystem (mounted read/write into obsidian-reader).

## Data locations (Docker volumes)

| Volume | Purpose |
|---|---|
| `agentibrain_pg_data` | Postgres data directory. |
| `agentibrain_redis_data` | Redis AOF. |
| `agentibrain_minio_data` | MinIO object store. |

Your vault lives on the host (`--vault` path) — not in a Docker volume.

## MinIO admin

Console: http://localhost:9001
Credentials come from `MINIO_ROOT_USER` / `MINIO_ROOT_PASSWORD` env vars; the
defaults (`agentibrain` / `agentibrain`) are fine for local dev. Override for
anything multi-user.

## Moving to AWS later

Nothing is locked in. Re-run `brain init` without `--local` and with
`--s3-bucket`; `brain up` will render a new compose without MinIO. Migrate the
MinIO bucket contents to S3 ahead of the switch using `mc mirror`.
