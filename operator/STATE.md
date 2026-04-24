---
id: agentibrain-kernel-state
title: agentibrain-kernel — State
project: agentibrain-kernel
status: active
maturity: 60%
updated: 2026-04-22
---

# agentibrain-kernel — State (2026-04-22, ~60%)

## Shipped

### Phases 1-6 (v0.1.0 tag)
- Python package `agentibrain` — CLI (`brain init/up/down/status/scaffold/version`), bootstrap, compose/helm templates.
- Scaffold — dynamic tree walk over `templates/vault-layout/`, ships 30 folders + 52 files mirroring the operator's real vault (dual hemispheres, bridge, identity, MUBS templates).
- 4 services — kb-router (ingest), obsidian-reader (vault read), embeddings (OpenAI text-embedding-3-small), tick-engine (2h tick, hybrid deterministic+AI).
- K8s deploy — 5 StatefulSets live in `anton-dev` (all 1/1 Running).
- Docs — architecture under `docs/architecture/` (antoncore's copies carry deprecation headers).

### Phase 7 (dev branch, PRs pending)
- **Stream 1** — kernel HTTP contract. 4 endpoints on kb-router:
  - `GET  /feed`    → hot_arcs + inject_blocks + entries (parsed from `brain-feed/*.md` frontmatter)
  - `GET  /signal`  → amygdala state (active, severity, hash, last_updated)
  - `POST /marker`  → routes lesson/milestone/signal/decision to correct vault paths, X-Idempotency-Key supported
  - `POST /tick`    → file-protocol request enqueue; `GET /tick/{id}` status
  - 25 pytest tests green; live-smoke green on `agentibrain-kb-router-0`.
- **Stream 2** — agentihooks HTTP client. `hooks/_brain_http.py` + HTTP branches in `brain_adapter`, `amygdala_hook`, `brain_writer_hook`. FS fallback preserved. 14 new tests, 745-test suite green.
- **Stream 3** — unvendor. Kernel is canonical source of `brain-keeper` agent + `brain`+`brain-keeper` profiles. `scripts/sync-from-kernel.sh` in agentihub + agentihooks-bundle, with drift-check CI on PR + weekly cron.
- **Stream 4 pre-work** — `agentibrain-parity` CronJob live, hourly probe of 7 endpoints from inside the cluster; green on first run.
- **Stream 4A** — `mcp-artifact-store` dev flipped from `anton-embeddings` to `agentibrain-embeddings`. Confirmed reach: 446 vectors.

### Infrastructure state
- Dev ArgoCD apps: 5 agentibrain-* apps synced + healthy, tracking `:dev` tag (kb-router) or `:latest` (others).
- Vault NFS mount on `agentibrain-kb-router-0` at `/vault` — Stream 1 read/write validated.
- Legacy Docker stack: `anton_obsidian_reader` still on `:8101` (dev still consumes). `anton_kb_router` already decommissioned.
- Legacy K8s: `anton-embeddings` StatefulSet still running dev + prod (LB 10.10.30.204 / 10.10.30.203).

## Not shipped (60% → 100%)

See `ENHANCEMENTS.md` for the full Tier 1-5 roadmap.

## Maturity scoring

| Axis | % | Notes |
|---|---|---|
| Feature surface | 80 | 4 services + 4 endpoints shipped, tick consumer missing |
| Pipeline | 85 | HTTP contract live, E2E from dispatched agents not yet validated |
| Observability | 60 | OTel spans present, no dashboards/alerts yet |
| Self-healing | 20 | Parity harness is new, no auto-remediation |
| Resilience | 40 | No backup or DR playbook documented |
| Docs | 70 | Architecture in `docs/`, quickstart missing |
| Distribution | 30 | Tag `v0.1.0` exists, PyPI untested, Helm not published |
| Prod | 50 | Dev clean, prod untouched |

**Weighted average: ~60%.**
