---
id: agentibrain-kernel-state
title: agentibrain-kernel — State
project: agentibrain-kernel
status: active
maturity: 80%
updated: 2026-04-30
---

# agentibrain-kernel — State (2026-04-30, ~80%)

## What's still open

See `BLOCKS.md` for the active block list and `ENHANCEMENTS.md` for the Tier 3-5 backlog. Top-of-mind:

- **Block 1** — closed 2026-05-03. PyPI publish + downstream pin bumps descoped (kernel reaches consumers via Helm + image, not pip). 1E legacy-dir cleanup verified done on disk.
- **Block 2** — prod cutover (dev→main on antoncore + kernel flips prod ArgoCD source from kernel to antoncore). Next gate.
- **Block 5** — decoupling residuals (kernel docs anton-namespace scrub, `examples/` tree, brain-cron Degraded diagnosis). Opportunistic.
- **Block 3** — friend-install story. Effectively paused (gated on PyPI publish, which is descoped). Pull from Tier 3 backlog when external adoption becomes a priority.

## Maturity scoring

| Axis | % | Notes |
|---|---|---|
| Feature surface | 95 | 4 services + HTTP contract + tick consumer + gateway contract all shipped |
| Pipeline | 90 | E2E from kb-router → LiteLLM → claude-max-haiku verified post-cutover |
| Observability | 60 | OTel spans present, no dashboards/alerts yet |
| Self-healing | 30 | Parity harness + retry hooks; no auto-remediation |
| Resilience | 40 | No backup or DR playbook documented |
| Docs | 75 | Architecture + GATEWAY-CONTRACT.md + portability docs shipped; quickstart still missing; doc-bleed in SECRETS/TROUBLESHOOTING/OPERATIONS |
| Distribution | 35 | PyPI publish descoped 2026-05-03 — Helm chart + container image distribution is sufficient for current fleet. OCI Helm publish still on Tier 3 backlog. |
| Prod | 50 | Dev cutover complete; prod still kernel-sourced until dev→main merge |

**Weighted average: ~80%.**

## Boundary state (2026-04-30)

- Kernel deployment-artifact bleed: **zero**. `helm/`, `services/`, `operator/values-overlays/` (deleted) and `k8s/argocd/` (deleted) carry no anton/claude-max/openbao tokens. Kernel is generic and clone-and-deploy.
- Antoncore owns: `k8s/values-overlays/agentibrain-*/` (5 overlay dirs), `k8s/argocd/{dev,prod}/agentibrain/` (6 ArgoCD `Application` CRs each), `agentibrain-root-{dev,prod}` repointed at antoncore's own subdir.
- Brain HTTP contract: standard OpenAI chat-completions to any compatible gateway (LiteLLM, OpenAI, Ollama). See `docs/GATEWAY-CONTRACT.md` and `operator/brain-models.yaml`.
- Brain-blind boundary: artifact-store no longer auto-embeds; brain writes happen only via deliberate `kb_ingest` MCP tool calls.
