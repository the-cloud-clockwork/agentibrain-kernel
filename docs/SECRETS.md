---
title: Secrets
parent: Operate
nav_order: 4
---

# Secrets — How They Flow

The kernel never stores credentials in code or images. Operators have **two
supported paths** depending on whether they run a secret manager:

1. **Without ESO — plain Opaque Secrets** (simplest; first-class supported)
2. **With ESO — your secret store as the source of truth** (GitOps-friendly; works with any ESO-supported backend: HashiCorp Vault, OpenBao, AWS Secrets Manager, GCP Secret Manager, Azure Key Vault, etc.)

Pick whichever fits your cluster. The kernel charts ship with `externalSecret.enabled: false` by default, so the Opaque-Secret path works out of the box on a fresh cluster.

## Path 1 — Without ESO (plain Opaque Secret)

The kernel ships [`local/k8s-bootstrap.sh`](../local/k8s-bootstrap.sh) — a
helper that mints random tokens and creates the three Opaque Secrets the
charts expect:

```bash
./local/k8s-bootstrap.sh                       # dry-run — prints commands
./local/k8s-bootstrap.sh --apply -n <your-ns>  # actually creates the Secrets
```

The script creates:
- `agentibrain-router-secrets` — `KB_ROUTER_TOKEN`, `EMBEDDINGS_API_KEY`
- `embeddings-secrets` — `POSTGRES_URL`, `LLM_API_KEY`, `LLM_API_BASE`, `LLM_EMBED_MODEL`, `EMBEDDINGS_API_KEYS`
- `agenticore-secrets` — only consumed by brain-keeper

Tokens land in `local/.k8s-tokens` (gitignored) so you can re-source them
when wiring agent fleet pods. Re-running the script is idempotent.

After the Secrets exist, `helm install` for each chart works without
touching ExternalSecrets at all. This is the path most external users take.

The rest of this doc covers Path 2 — ESO + your secret store — for
deployments that prefer GitOps-tracked secret rotation.

---

## Path 2 — With External Secrets Operator (ESO)

ESO + your secret store + K8s Secret. Secrets live once in the secret
store, ESO syncs them into K8s Secrets every 30 s, pods consume them via
`envFrom`. Flip `externalSecret.enabled: true` in your chart overlay to
enable.

### End-to-end flow

```
              (you write once, GitOps-tracked)
                       │
                       ▼
  ┌─────────────────────────────────────────┐
  │  Your secret store                      │
  │  (Vault / OpenBao / AWS SM / GCP SM /    │
  │   Azure KV — anything ESO supports)      │
  │  path: <your-prefix>/embeddings          │
  └────────────────┬────────────────────────┘
                   │ ESO polls every 30 s via ClusterSecretStore
                   ▼
  ┌─────────────────────────────────────────┐
  │  ExternalSecret CR (chart-managed)       │
  │  agentibrain-embeddings-secrets          │
  │  references store path + target name     │
  └────────────────┬────────────────────────┘
                   │
                   ▼
  ┌─────────────────────────────────────────┐
  │  K8s Secret  embeddings-secrets          │
  │  (same fields, base64-encoded)           │
  └────────────────┬────────────────────────┘
                   │ envFrom in StatefulSet spec
                   ▼
  ┌─────────────────────────────────────────┐
  │  agentibrain-embeddings-0 pod            │
  └─────────────────────────────────────────┘
```

## Two distinct secret bundles

### Bundle A — embeddings env vars

Store path: whatever convention your platform uses (e.g.
`<your-prefix>/embeddings` or `<your-prefix>/embeddings-dev`).
Owner is the ExternalSecret managed by the `agentibrain-embeddings` Helm chart.
Target K8s Secret name `embeddings-secrets`.
Consumed by `agentibrain-embeddings-0` pod via `envFrom`.

Six fields land as env vars: postgres connection string, LiteLLM proxy URL, embedding model name, the LiteLLM consumer key, inbound bearer keys for the embeddings service, log level.

### Bundle B — kb-router bearer token

K8s Secret name `agentibrain-router-secrets` in each namespace.
Holds the bearer token used by the kb-router and by every agent fleet pod that calls it. Either kubectl-create it directly, or — if you run ESO — add a path for it to your secret store and switch the chart's `externalSecret.enabled: true`.

## ESO requirements

Your cluster must have External Secrets Operator installed and a
`ClusterSecretStore` (or per-namespace `SecretStore`) configured for whichever
backend you use. ESO supports Vault, OpenBao, AWS Secrets Manager, GCP
Secret Manager, Azure Key Vault, Kubernetes Secrets, and others. Pick one,
configure ESO per its [installation guide](https://external-secrets.io/),
and reference its name in the chart's `externalSecret.storeName` value.

The kernel chart only emits the `ExternalSecret` CR (declaring "pull these
keys from this path → write to this Secret"). Backend choice and
authentication are entirely yours.

## Populating the store

Use whatever workflow your secret store supports — its own CLI/UI, a
GitOps-driven sync (e.g. SOPS + git), Terraform, or manual. The kernel
doesn't dictate. Once the path is populated, ESO syncs within 30 s and the
K8s Secret appears.

## Rotating

1. Update the value at the store path (via your store's normal rotation tooling).
2. ESO refresh tick (≤ 30 s) updates the K8s Secret.
3. The Reloader controller (chart `podAnnotations.reloader.stakater.com/auto: "true"`) restarts pods that mount the Secret.
4. Without Reloader: `kubectl rollout restart sts/agentibrain-embeddings -n <your-namespace>`.

## Verifying

```bash
NS=<your-namespace>
kubectl -n $NS get externalsecret embeddings-secrets \
  -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}'
# expect: True

kubectl -n $NS get secret embeddings-secrets \
  -o jsonpath='{.data}' | python3 -c 'import json,sys;print(len(json.load(sys.stdin)))'
# expect: 6
```

## Anti-patterns

- kubectl-create the Secret directly when ESO is available — works but bypasses GitOps.
- Bake credentials into the image. Kernel images contain zero secret material.
- Put credential values in `values.yaml`. Reference a store path instead.
- Share one high-privilege auth token across all ESO backends. Use dedicated read-only credentials scoped to the kernel's prefix.

## Failure modes

See `TROUBLESHOOTING.md` § "ExternalSecret in SecretSyncedError".
