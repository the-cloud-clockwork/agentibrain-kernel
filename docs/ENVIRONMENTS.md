# Environments

> **Note:** This doc walks through a reference dev/prod split (namespaces
> `<your-dev-ns>`, `<your-prod-ns>`, `<your-ops-ns>`; LiteLLM at
> `litellm.<your-prod-ns>.svc`; OpenBao at `<your-secret-store-ip>`) as a
> concrete example. **None of those names are baked into the kernel.**
> Substitute your own namespaces, gateway URLs, and host addresses everywhere
> — the kernel charts default to empty / placeholder values and accept
> whatever your overlay sets. See `docs/DEPLOYMENT.md` for the generic
> placeholders.

The kernel is deployed twice in the reference setup: `<your-dev-ns>` and `<your-prod-ns>`. This doc lays out what differs and how the values overlay achieves it.

## What separates dev from prod

| Axis | dev | prod |
|---|---|---|
| K8s namespace | `<your-dev-ns>` | `<your-prod-ns>` |
| Cron namespace | `<your-ops-ns>` (shared) | `<your-ops-ns>` (shared, `-prod` suffix on CR name) |
| Image tag | `:dev` | `:latest` |
| ArgoCD source branch | `dev` | `main` |
| ArgoCD app CR names | un-suffixed (`agentibrain-kb-router`) | `-prod` suffix (`agentibrain-kb-router-prod`) |
| OpenBao path | `secret/k8s/embeddings-dev` | `secret/k8s/embeddings` |
| Vault NFS path | shared (single dual-hemisphere vault) | shared |
| LiteLLM service URL | `litellm.<your-prod-ns>.svc` (intentional — only one LiteLLM) | `litellm.<your-prod-ns>.svc` |
| MetalLB IP for embeddings | none (ClusterIP only) | `<your-cluster-ip>` |
| Replica count | 1 | 1 (scale up if load demands) |
| Resource limits | smaller (cpu 300m, mem 512Mi) | normal (cpu 500m, mem 1Gi) |

## Why the namespaces split

Two reasons:
1. **Blast radius** — testing a kernel image change in dev shouldn't touch prod agents.
2. **Parallel evolution** — the dev branch can be ahead of main; both run side-by-side with their own tags.

## Why the OpenBao path doesn't have a `-prod` suffix

Historical artifact. The legacy implementation only had a single embeddings service, so its OpenBao path was just `secret/k8s/embeddings`. Dev came later and got the `-dev` suffix. Renaming prod to `secret/k8s/embeddings-prod` for symmetry is on the Tier 4 backlog (see `operator/ENHANCEMENTS.md`).

## Values overlay pattern

Every kernel chart in this repo follows:

```
values.yaml          ← dev defaults (env=dev, image tag :dev, cluster URLs <your-dev-ns>.svc)
values-prod.yaml     ← prod overlay (env=prod, image tag :latest, cluster URLs <your-prod-ns>.svc)
```

ArgoCD apps reference both:

```yaml
helm:
  valueFiles:
    - values.yaml
    - values-prod.yaml
```

Helm merges deeply: maps merge by key, lists replace entirely. If your prod overlay defines `env.extra:`, it REPLACES the base list. Reproduce the base entries plus prod-additions, or your env vars vanish.

## ArgoCD app naming convention (CRITICAL)

CRs in `k8s/argocd/dev/` use un-suffixed names:
```yaml
metadata:
  name: agentibrain-kb-router
```

CRs in `k8s/argocd/prod/` use `-prod` suffix:
```yaml
metadata:
  name: agentibrain-kb-router-prod
```

If both have the same name, `app-of-apps-dev` (which reads `dev/`) and `app-of-apps-prod` (which reads `prod/`) will fight over the same K8s CR. Whoever syncs last wins; the loser's pods get pruned.

## Promoting dev → prod

The kernel itself: tag a release on `main` once dev burned in.
The deployment: ArgoCD on prod tracks `main`. Once a PR merges to main, prod auto-syncs (no manual promotion).

For a config-only change (no image rebuild needed):
1. Edit `values-prod.yaml` on `dev` branch
2. PR `dev` → `main`
3. Merge → ArgoCD applies

For an image change:
1. Push to `dev` branch → CI builds `:dev`
2. Test in `<your-dev-ns>`
3. PR `dev` → `main` → CI builds `:latest`
4. ArgoCD image-updater bumps the `agentibrain-X-prod` app digest
5. Pod rollout

## Running with a single environment

If you only have one cluster (no dev/prod split):
1. Skip `values-prod.yaml` — single `values.yaml` is enough.
2. Use one ArgoCD app per service, tracking `main`.
3. Set `BRAIN_URL` on agents to the single namespace.

The kernel doesn't require dev/prod separation; the reference setup splits them for blue-green safety.

## Local laptop install

Use `./local/bootstrap.sh && docker compose up -d` from the repo root (see [`../local/README.md`](../local/README.md)). The compose stack ships its own Postgres, Redis, MinIO. No K8s, no namespaces. The kernel runs in a single-environment mode.

## Multi-tenant brain

Out of scope for v0.1.x. Each operator runs their own kernel + vault. If you need multi-tenant: separate vault paths, separate OpenBao prefixes, multiple kb-router instances. Possible but not packaged.
