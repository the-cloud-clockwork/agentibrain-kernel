---
id: agentibrain-kernel-vision
title: agentibrain-kernel — Vision
project: agentibrain-kernel
status: active
created: 2026-04-22
---

# agentibrain-kernel — Vision

## What it is

The 6th agenti* ecosystem pillar. A standalone, Helm-chart + container-image (and optionally pip-installable) brain + knowledge-base kernel that packages everything that used to live inside antoncore's brain stack (kb-router, embeddings, tick-engine, brain-keeper, brain profile) as one shippable unit.

**Repo:** `github.com/The-Cloud-Clockwork/agentibrain-kernel`
**Tag (current):** `v0.1.1` (HTTP contract v1, brain-blind boundary, generic OpenAI gateway contract, decoupling cutover)
**Live deploy (operator reference):** 4 pods per env — `agentibrain-{kb-router,embeddings,brain-keeper,mcp}-0` in dev + prod, plus the singleton `brain-cron` CronJob set + `amygdala` Deployment in `<your-ops-namespace>`.

## What "100% mature" means

A friend can `pip install agentibrain`, run `brain init` + `brain up`, and stand up the full brain on their own machine (local compose OR k8s) with their own vault, their own LLM keys, their own embeddings provider — zero dependency on antoncore. Concretely:

1. **Shippable** — Helm chart + Docker images on GHCR with semver tags. PyPI publish wired (`publish.yml`) and dormant; lights up when external pip-install adoption is in scope.
2. **Dev+prod parity** — kernel runs in both `anton-dev` and `anton-prod`, legacy `anton-*` brain stacks fully retired.
3. **Self-install** — README quickstart works end-to-end on a fresh machine with <10 minutes of operator input.
4. **Observable** — Grafana dashboards + Prometheus alerts for all 4 services; vault write audit trail queryable.
5. **Resilient** — documented backup + restore playbook for vault + embeddings DB; disaster-recovery runbook.
6. **HTTP-native** — every agent pod in the fleet reads from `/feed`, `/signal`; writes through `/marker`; filesystem paths fully deprecated.
7. **Tested** — `tests/e2e/` proves the full pipeline (ingest → classify → embed → feed) against a real compose stack on every CI run.
8. **Tick loop closed** — `/tick` requests are consumed by tick-engine and move through `requested/→completed/` automatically.

## Why it matters

Antoncore's brain grew organically inside a monorepo and couldn't be extracted without carrying the whole stack. The kernel forces a clean boundary: what's "brain" and what's "operator-specific deployment values". It's also the first step toward a shareable artifact — the brain can teach other operators the same dual-hemisphere + MUBS structure that lifts individual productivity.

## Non-goals

- Not a replacement for antoncore — antoncore keeps agent definitions, deployment overlays, cross-service glue, and production configuration. The kernel is only the brain layer.
- Not an Obsidian plugin. Obsidian is an optional human UI on top of the markdown tree; the kernel runs without it.
- Not a multi-tenant service. One vault per kernel instance.
