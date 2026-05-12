# helm/

Helm charts for deploying the brain on Kubernetes. The kernel is the **canonical and exclusive owner** of every brain chart — downstream platform repos consume them via ArgoCD multi-source, providing only operator-specific values.

## Charts

| Chart | Purpose | Templates |
|---|---|---|
| `kb-router` | HTTP front door + vault read/write (`/feed /signal /marker /tick /index_artifact /ingest /search /vault/search`). | `tcc-k8s-service-template` v0.3.4 |
| `embeddings` | pgvector wrapper (write surface only — `/embed`). | `tcc-k8s-service-template` v0.3.4 |
| `mcp` | MCP server exposing brain + KB retrieval tools to agents. | `tcc-k8s-service-template` v0.3.8 |
| `brain-keeper` | Brain-ops agent (runs on agenticore image). | `tcc-k8s-service-template` v0.3.4 |
| `brain-cron` | Scheduled tick (CronJob) + amygdala signal consumer (Deployment) + tick-drain (CronJob). | Custom 3-template chart, no base. |

`tcc-k8s-service-template` is pulled from `oci://ghcr.io/the-cloud-clockwork` at chart-build time (`helm dep update`). The `.tgz` is vendored under each chart's `charts/` for offline use.

All charts version `0.1.1` (current). Bump on every breaking change to chart shape.

## Consumption — ArgoCD multi-source (recommended)

```yaml
spec:
  sources:
    - repoURL: https://github.com/The-Cloud-Clockwork/agentibrain-kernel.git
      targetRevision: v0.1.1
      path: helm/<chart>
      helm:
        valueFiles:
          - $values/k8s/values/agentibrain-<chart>/values.yaml
          - $values/k8s/values/agentibrain-<chart>/values-<env>.yaml
    - repoURL: https://github.com/<your-org>/<your-deployer-repo>.git
      targetRevision: <branch>
      ref: values
```

Requires ArgoCD ≥ 2.6. See `docs/DEPLOYMENT.md` Pattern B.

## Consumption — direct `helm install`

```bash
cd agentibrain-kernel
helm dep update helm/<chart>
helm upgrade --install <release> ./helm/<chart> \
  --namespace <ns> --create-namespace \
  --values your-values.yaml
```

## Values layout

- `values.yaml` — **generic defaults only**. Operator-specific bits (NFS server IPs, secret names, namespace-coupled URLs, image tags) MUST be overridden by the consumer.
- Per-chart override your consumer needs to provide:
  - `BRAIN_URL` (agents → kb-router)
  - `ARTIFACT_STORE_URL` (brain-keeper → operator's storage plane)
  - NFS / PVC mounts for the vault
  - Secret references (KB_ROUTER_TOKEN, OPENAI_API_KEY, EMBEDDINGS_API_KEY)
  - Image tag (default `latest`; override to pin)

## ArgoCD image-updater

The kernel publishes `agentibrain-*` images via `.github/workflows/docker-build.yml` on every push to `main` (`:latest`) and `dev` (`:dev`). Consumers add an Application label + annotations to track them:

```yaml
metadata:
  labels:
    image-updater/image: agentibrain-kb-router
  annotations:
    argocd-image-updater.argoproj.io/image-list: router=ghcr.io/the-cloud-clockwork/agentibrain-kb-router:latest
    argocd-image-updater.argoproj.io/router.update-strategy: digest
    argocd-image-updater.argoproj.io/router.helm.image-name: tpl.app.image.repository
    argocd-image-updater.argoproj.io/router.helm.image-tag: tpl.app.image.tag
```
