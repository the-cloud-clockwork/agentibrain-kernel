# Deployment

How to get the kernel running on a Kubernetes cluster. Three patterns, in order of preference.

## Patterns

### A — Vendored chart in your own repo (current operator pattern)
Copy `helm/brain-cron/` and `helm/brain-keeper/` from this repo into your own repo's `k8s/charts/` and write your own ArgoCD apps that point at them. You also write your own charts for kb-router, obsidian-reader, embeddings (the kernel ships only the brain-cron + brain-keeper helm assets today).

Pros: full control, operator-specific overrides live in your values. Drift caught at PR time.
Cons: kernel template changes don't auto-propagate — you sync manually.

### B — ArgoCD multi-source (recommended path forward)
Single ArgoCD app, two sources: kernel as base, your repo as overlay.

```yaml
spec:
  sources:
    - repoURL: https://github.com/The-Cloud-Clock-Work/agentibrain-kernel.git
      targetRevision: v0.1.0
      path: helm/brain-cron
      ref: kernel
    - repoURL: https://github.com/your-org/your-platform.git
      targetRevision: main
      ref: values
      path: deploy/agentibrain
      helm:
        valueFiles:
          - $values/values-prod.yaml
```

Pros: kernel pinned to a tag, your overlay separate. No drift.
Cons: requires ArgoCD ≥ 2.6.

### C — `helm install` direct
For non-GitOps installs:

```bash
helm install brain-cron \
  oci://ghcr.io/the-cloud-clock-work/charts/brain-cron \
  --version 0.1.0 \
  -n <your-ops-namespace> -f values-prod.yaml
```

Pros: simplest. Cons: no GitOps trail.

## Required cluster prerequisites

| Component | Purpose |
|---|---|
| Kubernetes ≥ 1.28 | Workload plane |
| MetalLB or another LB | If you want kernel embeddings exposed on a static IP for docker consumers |
| External Secrets Operator | Bridge OpenBao → K8s Secret |
| OpenBao or Vault | Operator-side secret store |
| Postgres + pgvector | Embeddings storage |
| NFS or shared PVC | Vault filesystem (single writer or RWX) |
| ArgoCD (optional) | If using GitOps |
| Reloader (optional) | Auto-restart pods on Secret/CM change |

## Operator-side state your install needs

Before pods come up, populate:

1. **OpenBao** path `secret/k8s/embeddings(-dev)` with the embeddings env vars
   (postgres URL, LLM endpoint, embed model, log level, bearer keys).
   See `SECRETS.md`.
2. **K8s ClusterSecretStore** named after your secret store (the operator reference uses `openbao`) pointing ESO at your secret-store instance. The kernel chart's ExternalSecret references it.
3. **K8s Secret** `agentibrain-router-secrets` in each namespace, with the kb-router bearer token. Either kubectl-create directly or wire another ES.
4. **NFS export or PVC** for `/vault`. Read-write from the kernel pods, read-only from agent pods if you mount the vault elsewhere.
5. **MetalLB IP pool** if you want a static LoadBalancer IP. Set the IP via `service.annotations.metallb.universe.tf/loadBalancerIPs` in your values overlay.

## Per-environment values overlay

Operator pattern: `values.yaml` is the dev base. `values-prod.yaml` is the prod overlay.

Example overlay (`values-prod.yaml` for agentibrain-embeddings):

```yaml
tpl:
  global:
    environment: prod
  app:
    image:
      tag: latest
  externalSecret:                        # nosecret
    awsSecretPath: k8s/embeddings        # nosecret  (populated OpenBao path)
  service:
    type: LoadBalancer
    annotations:
      metallb.universe.tf/loadBalancerIPs: "<your-cluster-ip>"
```

Helm merge semantics: maps merge, lists replace. If your base `values.yaml` has an `env.extra` list, your overlay's `extra:` will REPLACE it, not append. Reproduce the base list explicitly.

## Agent fleet wiring

Every agent pod that should talk to the kernel needs two env vars: `BRAIN_URL` (the kb-router service URL in-cluster) and `KB_ROUTER_TOKEN` (the bearer pulled from the K8s Secret). `agentihooks` reads both. With `BRAIN_URL` empty and the pod on K8s, agentihooks logs a critical warning.

```yaml
env:
  variables:
    BRAIN_URL: "http://agentibrain-kb-router.<ns>.svc:8080"
  extra:
  - name: KB_ROUTER_TOKEN
    valueFrom:
      secretKeyRef:                        # nosecret
        name: agentibrain-router-secrets   # nosecret
        key: KB_ROUTER_TOKEN
        optional: true
```

## Image strategy

Each kernel service has its own image, tagged per branch:

| Service | Image |
|---|---|
| kb-router | `ghcr.io/the-cloud-clock-work/agentibrain-kb-router:dev|latest` |
| obsidian-reader | `ghcr.io/the-cloud-clock-work/agentibrain-obsidian-reader:dev|latest` |
| embeddings | `ghcr.io/the-cloud-clock-work/agentibrain-embeddings:dev|latest` |
| tick-engine | `ghcr.io/the-cloud-clock-work/agentibrain-tick-engine:dev|latest` |
| brain-keeper | `ghcr.io/the-cloud-clock-work/agenticore:dev|latest` (uses agenticore base; brain-keeper is an agenticore agent, not its own image) |

Branch → tag: push to `dev` → `:dev`, push to `main` → `:latest`. ArgoCD image-updater watches the tag regex per app and bumps the digest.

## Smoke tests post-deploy

```bash
NS=<your-namespace>
URL=http://agentibrain-kb-router.$NS.svc:8080

kubectl -n $NS exec <agent-pod> -c agenticore -- sh -c \
  'curl -sS -o /dev/null -w "%{http_code}\n" --max-time 5 "$BRAIN_URL/feed" -H "Authorization: Bearer $KB_ROUTER_TOKEN"'
# expect 200
```

If 200, the install is wire-correct. See `OPERATIONS.md` for what to monitor next.
