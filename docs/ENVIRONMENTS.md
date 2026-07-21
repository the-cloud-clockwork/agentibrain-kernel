---
title: Environments
parent: Operate
nav_order: 3
---

# Environments

The kernel supports single-namespace and multi-namespace deployments. The simplest shape is a single namespace: `dev` is the working and deploy branch, `main` is the snapshot branch that deploys nothing, and your GitOps controller tracks `dev` directly. If you are running a multi-env setup the patterns below still apply — substitute your own namespaces. None of these names are baked into the kernel charts.

The reference multi-env setup deploys the kernel twice: `<your-dev-ns>` and `<your-prod-ns>`. This doc lays out what differs and how the values overlay achieves it.

## What separates dev from prod

| Axis | dev | prod |
|---|---|---|
| K8s namespace | `<your-dev-ns>` | `<your-prod-ns>` |
| Cron namespace | `<your-ops-ns>` (shared) | `<your-ops-ns>` (shared, `-prod` suffix on CR name) |
| Image tag | `:dev` | `:dev` |
| ArgoCD source branch | `dev` | `dev` |
| ArgoCD app CR names | un-suffixed (`agentibrain-brain-api`) | `-prod` suffix (`agentibrain-brain-api-prod`) |
| Secret-store path | `<your-prefix>/embeddings-dev` | `<your-prefix>/embeddings` |
| Vault NFS path | shared (single dual-hemisphere vault) | shared |
| LiteLLM service URL | `litellm.<your-prod-ns>.svc` (intentional — only one LiteLLM) | `litellm.<your-prod-ns>.svc` |
| LoadBalancer IP for embeddings | none (ClusterIP only) | `<your-cluster-ip>` |
| Replica count | 1 | 1 (scale up if load demands) |
| Resource limits | smaller (cpu 300m, mem 512Mi) | normal (cpu 500m, mem 1Gi) |

## Why the namespaces split

Two reasons:
1. **Blast radius** — testing a kernel image change in one namespace shouldn't touch agents in another.
2. **Parallel evolution** — multiple namespaces can run side-by-side with their own configuration overlays.

## Values overlay pattern

Every kernel chart in this repo follows:

```
values.yaml          ← dev defaults (env=prod, image tag :dev, cluster URLs <your-dev-ns>.svc)
values-prod.yaml     ← prod overlay (env=prod, image tag :dev, cluster URLs <your-prod-ns>.svc)
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
  name: agentibrain-brain-api
```

CRs in `k8s/argocd/prod/` use `-prod` suffix:
```yaml
metadata:
  name: agentibrain-brain-api-prod
```

If both have the same name, `app-of-apps-dev` (which reads `dev/`) and `app-of-apps-prod` (which reads `prod/`) will fight over the same K8s CR. Whoever syncs last wins; the loser's pods get pruned.

## Deploying changes

For a config-only change (no image rebuild needed):
1. Edit the relevant values overlay on `dev`
2. Push to `dev` → ArgoCD syncs

For an image change:
1. Push to `dev` → CI builds `:dev`
2. ArgoCD image-updater bumps digest → pod rollout

## Running with a single environment

If you only have one cluster:
1. Skip `values-prod.yaml` — a single `values.yaml` is enough.
2. Use one ArgoCD app per service, pointing at your working branch.
3. Set `BRAIN_URL` on agents to the single namespace.

The kernel does not require namespace separation; the reference setup splits them for blast-radius isolation.

## Local laptop install

Use `./local/bootstrap.sh && docker compose up -d` from the repo root (see [`../local/README.md`](../local/README.md)). The compose stack ships its own Postgres, Redis, MinIO. No K8s, no namespaces. The kernel runs in a single-environment mode.

## Multi-tenant brain

Out of scope for v0.1.x. Each deployment runs its own kernel + vault. If you need multi-tenant: separate vault paths, separate secret-store prefixes, multiple brain-api instances. Possible but not packaged.
