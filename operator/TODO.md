---
id: agentibrain-kernel-todo
title: agentibrain-kernel — TODO
project: agentibrain-kernel
status: active
updated: 2026-05-03
---

# agentibrain-kernel — TODO (next actions)

## Now

1. **24h prod log re-check (2026-05-04)** — count ERROR/FATAL/Traceback in `brain-api-prod` + `brain-keeper-prod` last 5000 lines vs today's baseline (brain-api=0, keeper=1). No new spikes ⇒ Block 2 fully closed.

## Soon

2. **E2E validation** — dispatch a dev agent, read its transcript, confirm it quoted a hot_arc from `/feed` in its context. Stream 4 close-out.

## Descoped (2026-05-03)

- ~~Block 1D PyPI publish + downstream pin bumps~~ — kernel reaches downstream via Helm + image, not pip. Friend-install / Tier 3 backlog.

## Later (Tier 3+)

See `ENHANCEMENTS.md`. Pull when prod parity has soaked, friend-install becomes priority, or Tier 4 hardening (e2e tests, dashboards, alerts, runbooks) is scheduled.

## Quick-reference commands

```bash
# Verify all dev brain apps Synced+Healthy
KUBECONFIG=~/.kube/config-k3s kubectl -n argocd get applications | grep agentibrain

# Smoke /feed against live brain-api (in-pod, picks up env)
KUBECONFIG=~/.kube/config-k3s kubectl -n anton-dev exec agentibrain-brain-api-0 -- \
  sh -c 'curl -fsSL -H "Authorization: Bearer ${KB_ROUTER_TOKEN}" http://localhost:8080/feed | head -c 500'
```
