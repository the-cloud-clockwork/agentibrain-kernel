# Kernel ArgoCD apps

Self-contained brain stack ArgoCD app definitions. **Forkable** — clone this
repo into your platform's GitOps tree and apply `root.yaml` to deploy the
full brain on your cluster.

## Layout

```
k8s/argocd/
├── dev/                                      # one app per service, dev env
│   ├── agentibrain-kb-router.yaml
│   ├── agentibrain-obsidian-reader.yaml
│   ├── agentibrain-embeddings.yaml
│   ├── agentibrain-brain-cron.yaml
│   ├── agentibrain-brain-keeper.yaml
│   └── mcp-agentibrain.yaml
├── prod/                                     # same set, prod env
│   └── …
└── root.yaml                                 # app-of-apps that fans out

operator/values-overlays/                     # per-env values overlays
├── agentibrain-kb-router/
│   ├── values.yaml
│   ├── values-dev.yaml
│   └── values-prod.yaml
└── …
```

The apps are multi-source: kernel's `helm/<chart>/` for the chart, and a
caller repo for the values overlay. The overlays in
`operator/values-overlays/` are the **operator (Anton) reference** — fork
them and replace IPs/paths/ESO config with your environment.

## Activation

These app YAMLs are **dormant by default** in this repo — they reference
the operator's antoncore values overlays. To activate:

1. Fork or replace the `repoURL` in each app to point at your overlay repo.
2. `kubectl apply -f k8s/argocd/root.yaml` once. The app-of-apps reconciles
   the per-service apps.
3. (operator) When ready to flip antoncore → kernel ownership, delete the
   matching apps from antoncore's `k8s/argocd/{dev,prod}/agentibrain-*` to
   avoid duplicate ArgoCD sync attempts on the same Kubernetes resource.

## Why this lives here

Per the brain-decoupling philosophy: the kernel owns brain lifecycle. App
definitions, helm charts, and reference value overlays travel with the
service that owns them. Operators consume the kernel; antoncore stops
shipping brain ArgoCD apps as a co-resident concern.

## See also

- `helm/*/values.yaml` — generic, portable defaults (Pass A hardening,
  2026-04-30)
- `local/k8s-bootstrap.sh` — token + secret bootstrap for fresh clusters
- `local/bootstrap.sh` + `compose.yml` — Docker Compose path (no K8s required)
- `docs/DEPLOYMENT.md` — multi-source ArgoCD patterns
