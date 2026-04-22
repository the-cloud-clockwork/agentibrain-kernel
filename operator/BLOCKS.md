---
id: agentibrain-kernel-blocks
title: agentibrain-kernel — Active Blocks
project: agentibrain-kernel
status: active
updated: 2026-04-22
---

# agentibrain-kernel — Active Blocks

## Block 1 — Phase 7 close-out (Tier 1, in-flight)

**Status:** in progress. Streams 1/2/3/4-prework/4A on dev branch, PRs not yet opened.

- [ ] Open 5 dev→main PRs (agentibrain-kernel, agentihooks, agentihub, agentihooks-bundle, antoncore) — blocked on operator PR signal (manifesto §15).
- [ ] 24h parity green — `agentibrain-parity` CronJob fires at minute 17 each hour. 24 consecutive green runs = Stream 4B+C unlock.
- [ ] Stream 4B — scale legacy `anton-embeddings` dev StatefulSet to 0. Keep manifests 48h.
- [ ] Stream 4C — delete ArgoCD apps `anton-embeddings` + `brain-keeper` + `brain-cron` (dev only), remove `stacks/{kb-router,obsidian-reader,anton-embeddings,brain-tools}/` dirs.
- [ ] Wire `BRAIN_URL` + `BRAIN_HTTP_TOKEN` env into every dev agent chart: agenticore, publisher, finops-agent, anton-agent, diagram-agent. Without this the hooks still hit the filesystem path.
- [ ] End-to-end validation: dispatch an agent with `BRAIN_URL` set, confirm `/feed` hot arc shows up in its CLAUDE.md injection and emitted markers land via `/marker`.
- [ ] Tick-engine consumer — `brain_tick.py` watches `brain-feed/ticks/requested/`, runs `run_tick()`, moves file to `completed/` or `failed/`. Right now `/tick` enqueues with no consumer.
- [ ] Confirm `publish.yml` fired on `v0.1.0` tag → check PyPI for `agentibrain==0.1.0`.

## Block 2 — Prod cutover (Tier 2, queued)

**Status:** not started. Depends on Block 1.

- [ ] `values-dev.yaml` / `values-prod.yaml` split on all 5 kernel charts.
- [ ] Deploy `agentibrain-embeddings` + `agentibrain-kb-router` + `agentibrain-obsidian-reader` into `anton-prod`.
- [ ] Prod ArgoCD apps tracking `:latest` tag.
- [ ] Flip `EMBEDDINGS_URL` in prod `mcp-artifact-store.yaml` + Docker `stacks/artifact-store/compose.yml`.
- [ ] Scale legacy `anton-embeddings` prod StatefulSet to 0 after prod parity green.
- [ ] ESO migration for `agentibrain-router-secrets` → OpenBao-backed ExternalSecret at `secret/k8s/agentibrain-router-{dev,prod}`.
