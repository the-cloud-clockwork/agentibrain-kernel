---
id: agentibrain-kernel-state
title: agentibrain-kernel — State
project: agentibrain-kernel
status: active
maturity: 85%
updated: 2026-05-03
---

# agentibrain-kernel — State (2026-05-03, ~85%)

## What's still open

See `BLOCKS.md` for the active block list and `ENHANCEMENTS.md` for the Tier 3-5 backlog. Top-of-mind:

- **Block 1** — closed 2026-05-03. PyPI publish + downstream pin bumps descoped.
- **Block 2** — closed 2026-05-03 except 24h log re-check (calendar-gated to 2026-05-04). Prod smoke matrix all 2xx; antoncore brain-ops singleton fix shipped to main; legacy chart dirs absent.
- **Block 5** — closed 2026-05-03. Doc anton-scrub (12 files), `examples/` tree (8 overlays + 10 ArgoCD CRs + root), brain-ops Phase 3 AI failure root-caused (stale INFERENCE_API_KEY post-NVMe recovery) and fixed via new `rotate_file` dispatch in litellm-state.
- **Block 3** — friend-install story. Effectively paused (gated on PyPI publish, which is descoped). Pull from Tier 3 backlog when external adoption becomes a priority.

## Maturity scoring

| Axis | % | Notes |
|---|---|---|
| Feature surface | 95 | 4 services + HTTP contract (incl. `/index_artifact`) + tick consumer + gateway contract all shipped |
| Pipeline | 95 | E2E ingest → classify → embed → /feed verified prod 2026-05-03; brain-ops Phase 3 AI restored post key rotation |
| Observability | 60 | OTel spans present, no dashboards/alerts yet |
| Self-healing | 30 | Parity harness + retry hooks; no auto-remediation |
| Resilience | 40 | No backup or DR playbook documented |
| Docs | 85 | Architecture + GATEWAY-CONTRACT.md + portability docs + 12-file anton-scrub + `examples/` tree all shipped; quickstart still missing |
| Distribution | 35 | PyPI publish descoped — Helm chart + container image distribution is sufficient for current fleet. OCI Helm publish on Tier 3 backlog. |
| Prod | 95 | Antoncore main carries prod overlays + ArgoCD apps; 5 prod brain pods Running. Smoke green; brain-ops singleton fix shipped. Open: 24h log re-check 2026-05-04. |

**Weighted average: ~85%.**

## Boundary state (2026-05-03)

- Kernel deployment-artifact bleed: **zero**. `helm/`, `services/`, `docs/` (12 files scrubbed today), and operator/ tree carry no anton/claude-max/openbao tokens outside `docs/ENVIRONMENTS.md` (kept as the operator-reference walk-through with a generic disclaimer). Kernel is generic and clone-and-deploy.
- Antoncore owns: `k8s/values-overlays/agentibrain-*/` (5 overlay dirs), `k8s/argocd/{dev,prod}/agentibrain/` (5 prod + 6 dev ArgoCD `Application` CRs after singleton fix). `agentibrain-root-{dev,prod}` source antoncore's own subdir.
- Brain HTTP contract: standard OpenAI chat-completions to any compatible gateway (LiteLLM, OpenAI, Ollama). See `docs/GATEWAY-CONTRACT.md` and `operator/brain-models.yaml`.
- Brain-blind boundary: artifact-store no longer auto-embeds; brain writes happen only via deliberate `kb_ingest` MCP tool calls or brain-api `POST /index_artifact`.
- litellm-state ships rotate-single-file dispatch input (PR #7, 2026-05-03) for targeted virtual-key rotations like the brain-inference recovery.
