# helm/

Helm charts for deploying the brain on Kubernetes.

| Chart | Purpose |
|---|---|
| `brain-keeper` | The brain-keeper agent (runs on the agenticore image). First-class agent for enrichment, triage, replay. |
| `brain-cron` | Scheduled cognitive tick (CronJob) + amygdala continuous signal consumer (Deployment). |

Both charts depend on `tccw-k8s-service-template` (v0.3.4, bundled under
`charts/` for now; a future release will publish kernel charts independently).

## Install

```bash
# Prerequisite: the kernel services (kb-router, obsidian-reader, embeddings) are
# running somewhere the chart can reach (e.g. via a K8s Service, ExternalName, or
# a co-deployed compose stack). See services/ for those.

helm upgrade --install brain-keeper ./brain-keeper \
    --namespace brain --create-namespace \
    --values values-<your-env>.yaml

helm upgrade --install brain-cron ./brain-cron \
    --namespace brain \
    --values values-<your-env>.yaml
```

## Values files

- `values.yaml` — generic defaults. Safe to install but you'll want to override:
  - `BRAIN_URL` (points at kb-router)
  - `ARTIFACT_STORE_URL` (points at your storage plane)
  - NFS / PVC mounts for the vault
  - Secret references (OPENAI_API_KEY, KB_ROUTER_TOKEN, etc.)
- `values-anton.yaml` — the operator's production-reference config. NFS server
  `10.10.30.130`, anton cluster service DNS, anton-specific labels. Use this as
  an example of a full deployment; do not copy it verbatim.
- `values-anton-prod.yaml` (brain-keeper only) — anton prod overlay on top of
  `values-anton.yaml`.

## ArgoCD image-updater

Operators using ArgoCD Image Updater can add a label on their Application CR to
track the `agentibrain-*` image families:

```yaml
metadata:
  labels:
    image-updater/image: agentibrain-tick-engine  # or agenticore for brain-keeper
```

The kernel's `docker-build.yml` publishes `:latest` and `:sha-<commit>` tags on
every push to main, which image-updater picks up within its poll interval.
