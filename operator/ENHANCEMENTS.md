---
id: agentibrain-kernel-enhancements
title: agentibrain-kernel — Enhancements (Tier 3-5)
project: agentibrain-kernel
status: backlog
updated: 2026-04-30
---

# agentibrain-kernel — Enhancements (Tier 3-5)

> **Note (2026-05-03):** PyPI publish (Tier 3) explicitly descoped from Block 1.
> Kernel reaches downstream via Helm chart + container image, not pip. Pull from
> here only when external adoption / friend-install becomes a priority.

Tier 1+2 (dev+prod parity) live in `BLOCKS.md`. This file tracks the 14 items beyond
parity. As of 2026-04-30: dev cutover complete (kernel decoupled from antoncore,
operator-side overlays + ArgoCD apps lifted into antoncore). Prod cutover gated
on dev→main merge — see Block 1D + Block 2.

## Tier 3 — External adoption (friend-install story)

- [ ] **README quickstart** — `pip install agentibrain` → `brain init` → `brain up` walkthrough with real output. ~30 min.
- [ ] **Docker compose profile** for non-k8s users. Templates exist in `templates/compose/*.j2` — needs a documented tested path from `brain init --local` to a running stack. ~2 h.
- [ ] **Architecture SVG/mermaid** in README. 4 services + tick engine + vault layout, clickable component legend. ~1 h.
- [ ] **`api/openapi.yaml`** aligned with reality — /feed /signal /marker /tick specs currently lag. ~1 h.
- [ ] **`CONTRIBUTING.md`** + migration guide from existing antoncore installs. ~2 h.
- [ ] **Helm chart publishing** — OCI registry at ghcr.io/the-cloud-clock-work/charts. Lets friends `helm install agentibrain`. ~3 h.

## Tier 4 — Production hardening

- [ ] **Integration tests in `tests/e2e/`** (dir is empty today). Compose-up, POST `/ingest`, verify vault write + embedding + `/feed` entry. ~4 h.
- [ ] **Load test** — know the `/feed` throughput ceiling with N concurrent agent pods. ~2 h.
- [ ] **Prometheus rules + Grafana dashboards** — 4 services × (latency p50/p95/p99, error rate, vault FS latency, embedding queue depth). Ship as `k8s/charts/agentibrain-observability/`. ~6 h.
- [ ] **Backup strategy** — vault NFS snapshot cadence + embeddings Postgres `pg_dump` schedule + artifact-store S3 replication. Runbook. ~3 h.
- [ ] **Runbook** — rotate `KB_ROUTER_TOKEN`, reset vault, restore from backup, diagnose stuck tick. ~2 h.
- [ ] **SSE transport for `/signal`** behind feature flag. Current polling burns tokens — SSE drops idle cost to zero. ~4 h.

## Tier 5 — Feature completeness

- [ ] **Signal → Redis stream bridge** — writes to `amygdala-active.md` also XADD to `anton:events:brain` automatically. Today this is done by agentihooks `brain_writer_hook`; should be kernel-side. ~3 h.
- [ ] **Brain-keeper uses kernel `/marker`** — today it writes vault files directly, bypassing the idempotency + routing logic. ~2 h.
- [ ] **Auto-demote arcs by age** — cold arcs (heat=0, age>7d) move from `clusters/` to `frontal-lobe/unconscious/`. Half-implemented in tick-engine, needs finishing. ~4 h.

## Total effort estimate

Tier 3: ~10 h. Tier 4: ~21 h. Tier 5: ~9 h. **Tier 1+2 (critical path) not counted here** — see BLOCKS.md.

## Prioritization note

Tier 1+2 = prod parity. Must close first.
Tier 3 = needed BEFORE external announcement / PyPI release.
Tier 4 = needed BEFORE first external user runs the kernel unattended.
Tier 5 = nice-to-haves that improve the existing in-house experience.
