---
id: agentibrain-kernel-todo
title: agentibrain-kernel — TODO
project: agentibrain-kernel
status: active
updated: 2026-04-30
---

# agentibrain-kernel — TODO (next actions)

## Now (next gates)

1. **Block 2 close-out** — antoncore PR `chore/block2-close-prod-cutover` merge → ArgoCD prunes prod brain-cron App → re-verify all 6 prod brain apps Synced+Healthy. Smoke already green (2026-05-03). 24h observation re-check 2026-05-04.
2. **Block 5 — decoupling residuals** — opportunistic. Kernel docs anton-namespace scrub (11 files), `examples/` tree for forkers, diagnose stuck `brain_tick.py` 2h jobs (`0/1` at 4h+).

## Soon (post-prod-cutover)

3. **E2E validation** — dispatch a dev agent, read its transcript, confirm it quoted a hot_arc from `/feed` in its context. Stream 4 close-out.

## Descoped (2026-05-03)

- ~~Block 1D PyPI publish + downstream pin bumps~~ — kernel reaches downstream via Helm + image, not pip. Friend-install / Tier 3 backlog.

## Later (Tier 3+)

See `ENHANCEMENTS.md` and Block 5 (decoupling residuals) in BLOCKS.md.

## Quick-reference commands

```bash
# Verify all dev brain apps Synced+Healthy
KUBECONFIG=~/.kube/config-k3s kubectl -n argocd get applications | grep agentibrain

# Smoke /feed against live kb-router (in-pod, picks up env)
KUBECONFIG=~/.kube/config-k3s kubectl -n anton-dev exec agentibrain-kb-router-0 -- \
  sh -c 'curl -fsSL -H "Authorization: Bearer ${KB_ROUTER_TOKEN}" http://localhost:8080/feed | head -c 500'
```
