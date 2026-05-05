# agentibrain-kernel — Documentation Index

The kernel is a brain + KB substrate for AI fleets. This index is the entry point. Every doc here answers a single question.

## Start here
- [`README.md`](../README.md) — what it is, why, and the 5-minute install paths.
- [`GLOSSARY.md`](GLOSSARY.md) — terms used everywhere (MUBS, arc, signal, marker, tick, hemisphere).
- [`API.md`](API.md) — full HTTP contract: `/feed /signal /marker /tick /ingest`.
- [`MCP.md`](MCP.md) — kernel-owned MCP server (`brain_search_arcs`, `brain_get_arc`, `kb_search`, `kb_brief`).
- [`VAULT-SCHEMA.md`](VAULT-SCHEMA.md) — folder layout v1, region semantics, schema marker.

## Install
- [`../local/README.md`](../local/README.md) — Docker Compose laptop path: `./local/bootstrap.sh && docker compose up -d`.
- [`HELM-QUICKSTART.md`](HELM-QUICKSTART.md) — bare-cluster Helm install (no operator infra required). Pairs with `local/k8s-bootstrap.sh`.
- [`DEPLOYMENT.md`](DEPLOYMENT.md) — Kubernetes deploy patterns (Helm, ArgoCD, env-split values).
- [`ENVIRONMENTS.md`](ENVIRONMENTS.md) — dev / prod separation, what differs, the values-prod overlay pattern.

## Run
- [`OPERATIONS.md`](OPERATIONS.md) — day-2: monitoring, scaling, restart, drain, backup.
- [`SECRETS.md`](SECRETS.md) — two supported paths: plain Opaque Secrets, or your secret store via ESO.
- [`TROUBLESHOOTING.md`](TROUBLESHOOTING.md) — error → fix recipes for the top 15 things that break.

## Architecture
- [`architecture/README.md`](architecture/README.md) — start here for design.
- [`architecture/ARCHITECTURE.md`](architecture/ARCHITECTURE.md) — full kernel design: services, data plane, control plane.
- [`architecture/CLUSTERS.md`](architecture/CLUSTERS.md) — arc lifecycle (write → heat → graduate → demote).
- [`architecture/KEEPER.md`](architecture/KEEPER.md) — brain-keeper agent.
- [`architecture/MARKERS.md`](architecture/MARKERS.md) — marker grammar (`<!-- @lesson -->`, etc.).
- [`architecture/SYMBIOSIS.md`](architecture/SYMBIOSIS.md) — relationship to agenticore + agentihooks.
- [`architecture/TELEMETRY.md`](architecture/TELEMETRY.md) — OTel spans, ClickHouse, Langfuse.
- [`architecture/MATURITY.md`](architecture/MATURITY.md) — kernel maturity rubric.
- [`architecture/READERS-GUIDE.md`](architecture/READERS-GUIDE.md) — for new contributors.

## Reference
- [`../api/openapi.yaml`](../api/openapi.yaml) — OpenAPI 3 spec for the HTTP contract.
- [`../operator/`](../operator/) — operator MUBS: VISION, STATE, BLOCKS, ENHANCEMENTS, TODO.

## When stuck
1. `OPERATIONS.md` for routine ops
2. `TROUBLESHOOTING.md` for failure modes
3. Open an issue in `agentibrain-kernel` if neither covers it
