# Secrets — How They Flow

The kernel never stores credentials in code or images. Operators have **two
supported paths** depending on whether they run a secret manager:

1. **Without ESO — plain Opaque Secrets** (simplest; first-class supported)
2. **With ESO — OpenBao / AWS SM / Vault as the source of truth** (GitOps-friendly)

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
- `agentibrain-router-secrets` — `KB_ROUTER_TOKEN`, `OBSIDIAN_READER_TOKEN`, `EMBEDDINGS_API_KEY`
- `embeddings-secrets` — `POSTGRES_URL`, `LLM_API_KEY`, `LLM_API_BASE`, `LLM_EMBED_MODEL`, `EMBEDDINGS_API_KEYS`
- `agenticore-secrets` — only consumed by brain-keeper

Tokens land in `local/.k8s-tokens` (gitignored) so you can re-source them
when wiring agent fleet pods. Re-running the script is idempotent.

After the Secrets exist, `helm install` for each chart works without
touching ExternalSecrets at all. This is the path most external users take.

The rest of this doc covers Path 2 — ESO + OpenBao — for operators who
prefer GitOps-tracked secret rotation.

---

## Path 2 — With External Secrets Operator (ESO)

OpenBao + ESO + K8s Secret. Secrets live once in OpenBao, ESO syncs them
into K8s Secrets every 30s, pods consume the K8s Secrets via `envFrom`.
Flip `externalSecret.enabled: true` in your chart overlay to enable.

### End-to-end flow

```
                 (operator writes once, GitOps-tracked)
                       │
                       ▼
  ┌─────────────────────────────────────────┐
  │  OpenBao  secret/k8s/embeddings         │
  │  (pg url, llm endpoint, embed model,     │
  │   bearer keys, log level)                │
  └────────────────┬────────────────────────┘
                   │ ESO polls every 30 s via openbao ClusterSecretStore
                   ▼
  ┌─────────────────────────────────────────┐
  │  ExternalSecret CR (chart-managed)       │
  │  agentibrain-embeddings-secrets          │
  │  references OpenBao path + target name   │
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

OpenBao path is `secret/k8s/embeddings` (prod) or `secret/k8s/embeddings-dev` (dev).
Owner is the ExternalSecret managed by the `agentibrain-embeddings` helm chart.
Target K8s Secret name `embeddings-secrets`.
Consumed by `agentibrain-embeddings-0` pod via `envFrom`.

Six fields land as env vars: postgres connection string, LiteLLM proxy URL, embedding model name, the LiteLLM consumer key, inbound bearer keys for the embeddings service, log level.

### Bundle B — kb-router bearer token

No OpenBao path yet — currently kubectl-applied directly.
K8s Secret name `agentibrain-router-secrets` in each namespace.
Holds the bearer token used by the kb-router and by every agent fleet pod that calls it. Tier 4 hardening: move into OpenBao + ES so it survives a fresh redeploy without manual kubectl apply.

## ESO requirements

Your cluster must have External Secrets Operator installed and a `ClusterSecretStore` (named `openbao` in the operator reference; substitute your own). The CR points ESO at OpenBao with a token that has read access on `secret/k8s/*`. The token lives in a K8s Secret in the `external-secrets` namespace.

Standard ESO shape: `provider.vault` block, `path: "secret"`, `version: "v2"`, `auth.tokenSecretRef` referencing the secret-store auth token K8s Secret (named `openbao-token` in the operator reference).

## Populating OpenBao

The operator populates paths once via the bao CLI inside the OpenBao container. Read access is automatic via ESO; pods never touch OpenBao directly. After write, ESO syncs within 30 s.

## Rotating

1. Update the OpenBao path with `bao kv patch`.
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
- Put credential values in `values.yaml`. Reference an OpenBao path via `awsSecretPath:`.
- Share one OpenBao token across all ES backends. Use a dedicated read-only token on `secret/k8s/*`.

## Failure modes

See `TROUBLESHOOTING.md` § "ExternalSecret in SecretSyncedError".
