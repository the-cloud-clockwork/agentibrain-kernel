# services/

Runtime services shipped by `agentibrain-kernel`. Each service has its own
`Dockerfile` and publishes a container image to
`ghcr.io/the-cloud-clock-work/agentibrain-<name>` via
`.github/workflows/docker-build.yml`.

| Service | Port | Purpose |
|---|---|---|
| `kb-router` | 8080 (host 8102) | Universal ingest + federated search. FastAPI. |
| `obsidian-reader` | 8080 (host 8101) | Read/write interface to the Obsidian-compatible vault. |
| `embeddings` | 8080 | pgvector embedding service. |
| `tick-engine` | — (CronJob) | 15 scripts for the cognitive tick: extract / cluster / embed / heal / apply / markers. |

## Local solo-dev

Each service ships a per-service `compose.yml` that runs it in isolation on the
shared external network `agentibrain_net`:

```bash
docker network create agentibrain_net  # once
cd services/kb-router
docker compose up --build
```

For the full stack (all 4 services + Postgres + Redis + optional MinIO), use
the unified compose rendered by `brain up` (Phase 5).

## Env var convention

- Upstream service URLs default to short DNS names (`http://obsidian-reader:8080`)
  which work inside the kernel compose network.
- Secrets and required config (`OPENAI_API_KEY`, `KB_ROUTER_TOKEN`) come from
  env; services fail fast with a clear error if missing.
- Legacy `ANTON_*` env var aliases are stripped; BrainSettings (see
  `agentibrain/config.py`) is the authoritative schema.
