# Secrets — How They Flow

The kernel never stores credentials in code or images. Three things hold operator-supplied state: **OpenBao** (canonical), **External Secrets Operator** (bridge), **K8s Secret** (consumed by pods).

## End-to-end flow

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

Your cluster must have External Secrets Operator installed and a `ClusterSecretStore` named `openbao`. The CR points ESO at OpenBao with a token that has read access on `secret/k8s/*`. The token lives in a K8s Secret in the `external-secrets` namespace.

Standard ESO shape: `provider.vault` block, `path: "secret"`, `version: "v2"`, `auth.tokenSecretRef` referencing the openbao-token K8s Secret.

## Populating OpenBao

The operator populates paths once via the bao CLI inside the OpenBao container. Read access is automatic via ESO; pods never touch OpenBao directly. After write, ESO syncs within 30 s.

## Rotating

1. Update the OpenBao path with `bao kv patch`.
2. ESO refresh tick (≤ 30 s) updates the K8s Secret.
3. The Reloader controller (chart `podAnnotations.reloader.stakater.com/auto: "true"`) restarts pods that mount the Secret.
4. Without Reloader: `kubectl rollout restart sts/agentibrain-embeddings -n anton-prod`.

## Verifying

```bash
NS=anton-prod
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
