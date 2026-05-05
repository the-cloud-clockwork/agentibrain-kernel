# Troubleshooting

Top failure modes, ordered by how often they bite. Each entry: symptom, root cause, fix.

---

## 1. agent /feed returns connection refused / DNS failure

**Symptom:** `kubectl -n <ns> exec <agent-pod> -- curl $BRAIN_URL/feed` → `Could not resolve host` or `Connection refused`.

**Root cause:** kb-router pod not running, or service name wrong, or pod's BRAIN_URL points at the wrong namespace.

**Fix:**
```bash
NS=<your-namespace>
kubectl -n $NS get pod -l 'app.kubernetes.io/instance=agentibrain-kb-router-prod'
kubectl -n $NS get svc agentibrain-kb-router
kubectl -n $NS exec <agent-pod> -- env | grep BRAIN_URL
```
- If pod missing: ArgoCD app status, rollout, image-pull issues (see #4).
- If env var wrong: see `DEPLOYMENT.md` § "Agent fleet wiring".

---

## 2. /feed returns HTTP 401

**Symptom:** kb-router responds, but with 401.

**Root cause:** bearer token mismatch — agent's `KB_ROUTER_TOKEN` env var doesn't match the one kb-router was started with.

**Fix:**
```bash
# what kb-router expects
kubectl -n <your-namespace> get secret agentibrain-router-secrets \
  -o jsonpath='{.data.KB_ROUTER_TOKEN}' | base64 -d | head -c 12

# what the agent has
kubectl -n <your-namespace> exec <agent-pod> -- sh -c 'echo "$KB_ROUTER_TOKEN" | head -c 12'
```
If they don't match, restart the agent pod after fixing the chart values, OR update `agentibrain-router-secrets` to match what the chart deployed.

---

## 3. ExternalSecret in SecretSyncedError

**Symptom:** `kubectl get externalsecret embeddings-secrets -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}'` returns `False`.

**Root causes (in order):**
1. The path in your secret store doesn't exist or has zero keys.
2. ESO can't authenticate to your store (auth token expired or missing).
3. ESO can't reach your store network-wise.

**Fix:**
```bash
# describe shows the actual error
kubectl -n <your-namespace> describe externalsecret embeddings-secrets | grep -E "Reason|Message" | head

# verify the path in your secret store using its native CLI/UI

# check ESO auth
kubectl -n external-secrets logs deploy/external-secrets --tail=50 | grep -i auth
```

---

## 4. Pod stuck in CreateContainerConfigError

**Symptom:** `kubectl get pod` shows `CreateContainerConfigError`. Describe says "secret X not found".

**Root cause:** the K8s Secret the pod's `envFrom` references hasn't been created yet (ESO not synced) or was deleted.

**Fix:**
```bash
NS=<your-namespace>
kubectl -n $NS describe pod <pod-name> | grep -E "Warning|Error" | head
kubectl -n $NS get secret embeddings-secrets
# if missing:
kubectl -n $NS get externalsecret -A | grep embeddings
# wait 30s, then:
kubectl -n $NS delete pod <pod-name>
```
The Reloader controller (if installed) does this automatically.

---

## 5. tick-drain Job pods all "Failed"

**Symptom:** `kubectl -n <your-ops-namespace> get jobs | grep tick-drain` shows recent jobs as Failed.

**Root cause:** the tick-engine image is missing a Python module (Dockerfile drift), or the script crashed on a malformed request file in `ticks/requested/`.

**Fix:**
```bash
# read the latest pod's log
POD=$(kubectl -n <your-ops-namespace> get pod -o name | grep tick-drain | tail -1 | sed 's|pod/||')
kubectl -n <your-ops-namespace> logs $POD | tail -50
```
- `ModuleNotFoundError`: rebuild image (see PR #3 in kernel for the precedent — `COPY *.py ./`).
- malformed file: move the offending file out of `requested/` to `failed/` manually.

---

## 6. /tick request stays in `requested/` forever

**Symptom:** POST `/tick` returns 202 with a job_id, but the file in `brain-feed/ticks/requested/` never moves.

**Root cause:** tick-drain CronJob disabled or broken.

**Fix:**
```bash
kubectl -n <your-ops-namespace> get cronjob agentibrain-brain-cron-tick-drain
# Suspend=False, last successful time recent?
kubectl -n <your-ops-namespace> describe cronjob agentibrain-brain-cron-tick-drain | tail -20
```
If suspended: `kubectl -n <your-ops-namespace> patch cronjob agentibrain-brain-cron-tick-drain -p '{"spec":{"suspend":false}}'`.

---

## 7. ArgoCD app SyncError: "shared resource"

**Symptom:** `app-of-apps-prod` reports `SharedResourceWarning` or `Application X is part of multiple app-of-apps`.

**Root cause:** an ArgoCD Application CR has the same `metadata.name` in both `k8s/argocd/dev/` and `k8s/argocd/prod/`. They fight.

**Fix:** rename one. Convention: prod CRs end with `-prod` suffix. Dev CRs use the un-suffixed name.

See `MIGRATION.md` for the cutover-time variant of this bug.

---

## 8. Vault file written via /marker but agents don't see it in /feed

**Symptom:** `/marker` returned 201, the file is on NFS, but `/feed` doesn't include it.

**Root cause:** `/feed` doesn't read every vault file. It only reads `brain-feed/hot-arcs.md`, `brain-feed/inject.md`, `brain-feed/intent.md`, etc. Lessons + decisions land in their own directories and don't show up in feed until the next tick promotes a related arc.

**Fix:** wait for the next 2 h tick, OR force a tick: `kubectl -n <your-ops-namespace> create job --from=cronjob/agentibrain-brain-cron agentibrain-brain-cron-manual-$(date +%s)`.

---

## 9. Embeddings pod CrashLoop with "could not connect to Postgres"

**Symptom:** `agentibrain-embeddings-0` restarts with Postgres connection error.

**Root causes:**
1. `POSTGRES_URL` env var wrong — wrong host, wrong port, wrong creds.
2. Postgres host unreachable from the cluster.
3. pgvector extension not installed in the embeddings DB.

**Fix:**
```bash
NS=<your-namespace>
kubectl -n $NS exec agentibrain-embeddings-0 -- env | grep POSTGRES_URL | head -c 60
ssh <your-postgres-host> "docker exec <postgres-container> psql -U embeddings -d embeddings -c 'SELECT 1;'"
ssh <your-postgres-host> "docker exec <postgres-container> psql -U embeddings -d embeddings -c '\\dx vector'"
```

---

## 10. Amygdala broadcasting stale signal

**Symptom:** agentihooks `brain_adapter` injection contains an old signal that's been resolved.

**Root cause:** the signal file in `amygdala/` wasn't deleted/updated. Tick auto-tombstoning didn't trigger.

**Fix:**
```bash
ssh <your-vault-host> "ls <your-vault-path>/amygdala/"
# remove the resolved signal file:
ssh <your-vault-host> "rm <your-vault-path>/amygdala/<filename>.md"
# next /feed will refresh
```

---

## 11. brain-cron 2 h tick fails with "ImportError"

**Symptom:** `brain-cron` job fails. Pod log shows `ModuleNotFoundError`.

**Root cause:** tick-engine image stale — a Python module was added in source but the Dockerfile `COPY` line missed it.

**Fix:** kernel PR with `COPY *.py ./` (or explicit add). Force docker-build to rebuild `:latest`. ArgoCD image-updater picks up new digest.

---

## 12. Two agents writing the same marker collide

**Symptom:** duplicate entries in `lessons-YYYY-MM-DD.md`, or 409 from `/marker`.

**Root cause:** missing or duplicate `X-Idempotency-Key`.

**Fix:** clients MUST send a per-marker idempotency key. Pattern: `<session_id>-<marker_index>`. Replays return the original response with `idempotent_replay: true` — that's the contract, not an error.

---

## 13. blackbox probe shows agentibrain-embeddings-k8s-prod down

**Symptom:** Grafana / Alertmanager fires for the kernel embeddings probe.

**Root cause:** probe URL drifted vs reality. Check `k8s/charts/blackbox-exporter/values-targets.yaml` — port 8080, path `/health`, host `agentibrain-embeddings.<your-namespace>.svc.cluster.local`.

**Fix:** correct the URL in values-targets.yaml + redeploy blackbox.

---

## 14. ArgoCD app stuck "OutOfSync" for hours

**Symptom:** the app's source revision matches main HEAD, but app says OutOfSync.

**Root causes:**
1. A finalizer on a deleted CR is blocking sync.
2. A Helm template renders different output than what's in cluster.

**Fix:**
```bash
# inspect operationState message
kubectl -n argocd get app <app-name> -o jsonpath='{.status.operationState.message}'

# common: "waiting for deletion of X"
kubectl -n argocd patch app <app-name> --type=merge \
  -p '{"metadata":{"finalizers":[]}}'
```

---

## 15. <your-cluster-ip>:8080/health timing out

**Symptom:** an external docker consumer can't reach embeddings on its LoadBalancer IP.

**Root cause:** `agentibrain-embeddings` Service either lost its LoadBalancer type or your LB controller's IP binding broke.

**Fix:**
```bash
kubectl -n <your-namespace> get svc agentibrain-embeddings -o jsonpath='type={.spec.type} ip={.status.loadBalancer.ingress[0].ip}'
# expect: type=LoadBalancer ip=<your-cluster-ip>
```
If it shows ClusterIP, the values-prod.yaml LB config didn't apply — check ArgoCD sync state.

---

## When this doc isn't enough

- `OPERATIONS.md` for routine ops
- `architecture/ARCHITECTURE.md` for design context
- `operator/BLOCKS.md` for in-flight work
- Open an issue in `agentibrain-kernel`
