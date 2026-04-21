# agentibrain-kernel

Standalone brain + knowledge-base kernel for the **agenti ecosystem**
(agenticore / agentihooks / agentibridge / agentihub / agentipublish / **agentibrain**).

A pluggable memory + KB substrate for AI fleets. Friends can install it standalone
on their own hardware (MinIO fallback, zero AWS dependency). Operators can deploy
it on K8s with the bundled Helm charts.

## Quickstart — local / friend install

```bash
pip install agentibrain
brain init --local --vault ~/my-vault --openai-key $OPENAI_API_KEY
brain up        # docker compose up: brain services + Postgres + Redis + MinIO
brain scaffold  # seed vault folder layout
brain status    # health check
```

## Quickstart — AWS path

```bash
pip install agentibrain
brain init --vault ~/my-vault \
           --s3-bucket my-brain-bucket \
           --postgres-url postgres://... \
           --openai-key $OPENAI_API_KEY
brain up
brain scaffold
```

Both paths write config to `~/.agentibrain/config.yaml` and the compose file to
`~/.agentibrain/compose.yml`.

## What you get

- **kb-router** — universal ingest + federated search (FastAPI)
- **obsidian-reader** — vault reader for markdown + YAML frontmatter
- **embeddings** — pgvector embedding service
- **tick-engine** — hybrid deterministic + LLM cognitive tick (arc clustering, heat scoring, pruning)
- **brain-keeper** — first-class agent for brain operations (enrichment, triage, replay)
- **brain-cron** — scheduled ticks + amygdala emergency-signal daemon
- **Vault schema** — versioned folder layout seeded by `brain scaffold`
- **HTTP API** — single contract (`/ingest`, `/search`, `/brief`, `/dispatch`, `/feed`, `/signal`, `/marker`)

## Architecture

See [`docs/architecture/`](docs/architecture/) for design docs and
[`api/openapi.yaml`](api/openapi.yaml) for the HTTP contract.

## Status

v0.1.x — alpha. The kernel is the canonical source for:
- Brain services (kb-router, obsidian-reader, embeddings, tick-engine)
- Brain Helm charts
- brain-keeper agent definition
- Brain profile overlays for `agentihooks`

Downstream repos (`agentihub`, `agentihooks-bundle`, `antoncore`) clone these at
install time rather than maintaining their own copies.

## License

MIT
