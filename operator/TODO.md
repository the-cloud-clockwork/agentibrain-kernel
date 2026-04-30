---
id: agentibrain-kernel-todo
title: agentibrain-kernel — TODO
project: agentibrain-kernel
status: active
updated: 2026-04-30
---

# agentibrain-kernel — TODO (next actions)

## Now (operator-gated)

1. **Open dev→main PRs** — kernel + 4 downstream (agentihooks, agentihub, agentihooks-bundle, antoncore) bumping kernel pin. Operator-gated PR signal — one sentence "open the PRs" unblocks this. Block 1D.
2. **Cut `v0.1.0` tag on `agentibrain-kernel/main`** — confirm `publish.yml` fires, verify `agentibrain==0.1.0` on PyPI. Block 1D.
3. **Antoncore legacy chart cleanup** — delete `k8s/charts/anton-{kb-router,obsidian-reader,tick-engine}/` after kernel apps confirmed Synced+Healthy in `anton-dev`. Block 1E.

## Soon (post-merge to main)

4. **E2E validation** — dispatch a dev agent, read its transcript, confirm it quoted a hot_arc from `/feed` in its context. Stream 4 close-out.
5. **Prod cutover** — Block 2 in BLOCKS.md.

## Later (Tier 3+)

See `ENHANCEMENTS.md` and Block 5 (decoupling residuals) in BLOCKS.md.

## Quick-reference commands

```bash
# Verify all dev brain apps Synced+Healthy
KUBECONFIG=~/.kube/config-k3s kubectl -n argocd get applications | grep agentibrain

# Smoke /feed against live kb-router (in-pod, picks up env)
KUBECONFIG=~/.kube/config-k3s kubectl -n anton-dev exec agentibrain-kb-router-0 -- \
  sh -c 'curl -fsSL -H "Authorization: Bearer ${KB_ROUTER_TOKEN}" http://localhost:8080/feed | head -c 500'

# Check v0.1.0 tag + publish status
gh api repos/The-Cloud-Clock-Work/agentibrain-kernel/releases/tags/v0.1.0 2>/dev/null | head -20
pip index versions agentibrain  # or curl pypi.org/pypi/agentibrain/json
```
