# Migration — Swapping a Legacy Brain for the Kernel

This is the playbook for swapping a pre-kernel brain implementation (legacy custom services) for `agentibrain-kernel`. Captured here because every operator onboarding hits the same shape.

## Pre-conditions

You have:
- An existing brain implementation (filesystem reads, custom services, ad-hoc CronJobs).
- Agents (agenticore-style) already reading/writing the vault by some path.
- A working OpenBao + ESO + ArgoCD setup.

You want:
- Kernel services owning the HTTP contract (`/feed /signal /marker /tick`).
- Agents talking only to the kernel, not directly to the filesystem.
- Old services retired with no data loss.

## Block 1 — Dev cutover

### 1A — Wire BRAIN_URL into agent charts
For every agent, add:
```yaml
env:
  variables:
    BRAIN_URL: "http://agentibrain-kb-router.<ns>.svc:8080"
  extra:
  - name: KB_ROUTER_TOKEN
    valueFrom:
      secretKeyRef:
        name: agentibrain-router-secrets
        key: KB_ROUTER_TOKEN
        optional: true
```
Set `optional: true` so prod agents that don't yet have the secret keep starting cleanly.

### 1B — Smoke from inside an agent pod
Don't trust `curl` from your laptop. Test from where it actually matters:
```bash
kubectl -n <ns> exec <agent-pod> -c agenticore -- sh -c \
  'curl -sS -o /dev/null -w "%{http_code}\n" $BRAIN_URL/feed -H "Authorization: Bearer $KB_ROUTER_TOKEN"'
```
Run for `/feed`, `/signal`, `/marker`, `/tick`. Expect 200, 200, 201, 202.

### 1C — Stream 4B+C — retire legacy
With kernel responding from inside the fleet, scale legacy StatefulSets to 0, delete their ArgoCD apps with `cascade=foreground`, remove their chart dirs from the repo. Keep their Service shells alive ONLY if external consumers (docker stacks) point at them — see "Service alias bridge" below.

### 1D — PR + publish
Open dev→main PRs in: kernel, agentihooks, agentihooks-bundle, agentihub (any repo with kernel-touching code). Tag `v0.1.0` after merges land.

### 1E — Post-merge cleanup
Delete the legacy chart directories from your platform repo. ArgoCD prunes the orphans.

## Block 2 — Prod cutover

### 2A — Storage + secrets in prod
Mirror dev:
- Populate OpenBao path `secret/k8s/embeddings` with the prod values.
- Create `agentibrain-router-secrets` K8s Secret in `<your-namespace>` with the bearer token.
- Provision the NFS export / PVC for the prod vault.
- Reserve the MetalLB IP (e.g. <your-cluster-ip>) you want kernel embeddings to claim.

### 2B — Deploy
Open new ArgoCD apps under `k8s/argocd/prod/` for the 5 kernel services.
**Critical: name them with a `-prod` suffix to avoid collision with dev app CRs.** Copying dev YAMLs without renaming lets `app-of-apps-prod` hijack the dev CRs and prune dev pods.

### 2C — Client cutover
Three things flip from legacy → kernel:
1. **K8s consumers** of legacy embeddings/router services — update each chart's URL env var (e.g. `mcp-artifact-store` `EMBEDDINGS_URL`) to point at `agentibrain-embeddings.<your-namespace>.svc:8080`.
2. **Agent BRAIN_URL** — flip `values-prod.yaml` from empty string to `http://agentibrain-kb-router.<your-namespace>.svc:8080`.
3. **Docker / external consumers** — see "Service alias bridge" below.

### 2D — Smoke + retire
Smoke from prod agent pods (same matrix as 1B but `-n <your-namespace>`). After 24-48 h of green:
- scale legacy StatefulSets to 0
- delete legacy ArgoCD apps `cascade=foreground`
- delete legacy chart dirs

## Service alias bridge — for external (docker) consumers

If a non-K8s consumer (Unraid docker stack, external service) was hardcoded to a legacy service IP like `http://<your-cluster-ip>:8080`, you have two options:

### Option 1 — Move the IP to the kernel Service directly (clean)
Make the kernel embeddings Service a LoadBalancer with the static IP via MetalLB:
```yaml
service:
  type: LoadBalancer
  annotations:
    metallb.universe.tf/loadBalancerIPs: "<your-cluster-ip>"
```
Delete the legacy Service shell. Done.

### Option 2 — Keep the legacy-named Service as an alias
Patch the legacy Service's selector to point at kernel pods:
```bash
kubectl -n <your-namespace> patch svc <legacy-embeddings-svc> --type=merge -p \
  '{"spec":{"selector":{"app.kubernetes.io/instance":"agentibrain-embeddings-prod","app.kubernetes.io/name":"tpl"}}}'
```
The IP stays the same; traffic flows to kernel pods. Use this when you can't easily change the consumer's config.

A typical cutover uses Option 2 first (zero-config bridge), then Option 1 once everything is stable.

## Common gotchas

| Gotcha | Cause | Fix |
|---|---|---|
| Dev pods pruned mid-merge | Same-named ArgoCD CRs in dev/ + prod/ collide | Rename prod CRs with `-prod` suffix |
| Tick consumer never drains | `tick-drain` CronJob not deployed | Ship `helm/brain-cron/templates/tick-drain-cronjob.yaml` |
| `ModuleNotFoundError` on tick fire | Stale Dockerfile `COPY` listing | Use `COPY *.py ./` |
| ES kubectl-applied during recovery | Manual fix during cutover | Codify ES into chart before declaring done |
| OpenBao path rename silently fails | `patch_secret` MCP no-ops on new paths | Use bao CLI directly |
| External consumer breaks at IP swap | Legacy Service deleted before alias set up | Always create alias FIRST, delete shell SECOND |

## After the cutover

You're not done at "pods are running." Walk the decoupling rubric in `architecture/MATURITY.md` and address what's still coupled. Common residuals: kubectl-applied ES (codify), legacy-named OpenBao paths (rename), vendored chart drift (multi-source ArgoCD).
