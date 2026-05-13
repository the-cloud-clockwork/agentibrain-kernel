---
title: Helm Quickstart
parent: Operate
nav_order: 1
---

# Helm Quickstart

Bare-cluster install of the brain. The reference path lives in
`local/k8s-bootstrap.sh` — run it with `--help` for the current shape.

## Steps

1. Clone the repo.
2. Export your env (Postgres URL, embedding provider key) per
   `local/k8s-bootstrap.sh --help`.
3. `./local/k8s-bootstrap.sh --apply -n <namespace>` — mints tokens and
   creates the three Opaque Secrets the charts read.
4. Provision a vault PVC (or set up NFS via an overlay).
5. `helm install` each chart from `helm/` into your namespace.
6. Port-forward `svc/agentibrain-brain-api` and curl `/feed` with the
   bearer in `local/.k8s-tokens`.

## Chart defaults shipped

- Service URLs are namespace-less and resolve via in-cluster DNS.
- Optional integration URLs (inference gateway, artifact store) are empty.
- `imagePullSecrets: []` — ghcr.io is public.
- `externalSecret.enabled: false` — flip to `true` to wire in ESO.
- `storageClass: ""` — cluster default.
- `extraVolumes: []` — supply NFS or PVC via overlay.

## storageClass per cluster

| Cluster | value |
|---|---|
| k3s | `local-path` |
| kind | `standard` |
| minikube | `standard` |
| EKS | `gp2` or `gp3` |
| GKE | `standard` or `premium-rwo` |
| AKS | `default` or `managed-csi` |

`""` lets the cluster's default StorageClass apply — works on most modern
clusters with no override.

## Platform overlays

A typical production deploy layers NFS (or another RWX volume), ESO (with
your secret store of choice), and a LoadBalancer controller on top of these
defaults. See `examples/values-overlays/` for sample overlays you can copy
into your own deployment repo.

## Troubleshooting

- Pod `Pending` → check `storageClass` or that the PVC is bound.
- `embeddings` crashloop → Postgres unreachable or pgvector not enabled.
- `/feed` returns 401 → token mismatch; re-source `local/.k8s-tokens`.
- Empty `hot_arcs` → no clusters yet; wait for `brain-ops` to tick.

## See also

- `local/README.md` — Docker Compose path.
- `docs/DEPLOYMENT.md` — multi-source ArgoCD patterns.
- `k8s/argocd/README.md` — app-of-apps for full-stack ArgoCD deploy.
