# Helm value overlays

Bundled overlays that `brain` (future — Phase 7+) can apply on top of
`helm/brain-cron/values.yaml` and `helm/brain-keeper/values.yaml`.

For now this directory ships:

- `local-pvc.yaml` — minimal PVC-backed overlay (no NFS). Pair with
  `helm upgrade --install … -f agentibrain/templates/helm/local-pvc.yaml`.

Operators who want richer overlays (NFS, OpenBao secrets, environment-specific
image tags, etc.) keep them in their own deployment repo. The canonical charts
stay in `helm/brain-cron/` and `helm/brain-keeper/`.
