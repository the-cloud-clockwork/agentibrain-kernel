---
title: Deployment
parent: Operate
nav_order: 2
---

# Deployment

How to get the kernel running on a Kubernetes cluster. Three patterns, in order of preference.

## Patterns

### A — Vendored chart in your own repo (current operator pattern)
Copy `helm/brain-ops/` and `helm/brain-keeper/` from this repo into your own repo's `k8s/charts/` and write your own ArgoCD apps that point at them. You also write your own charts for brain-api, embeddings (the kernel ships only the brain-ops + brain-keeper helm assets today).

Pros: full control, operator-specific overrides live in your values. Drift caught at PR time.
Cons: kernel template changes don't auto-propagate — you sync manually.

### B — ArgoCD multi-source (recommended path forward)
Single ArgoCD app, two sources: kernel as base, your repo as overlay.

```yaml
spec:
  sources:
    - repoURL: https://github.com/The-Cloud-Clockwork/agentibrain-kernel.git
      targetRevision: v0.1.0
      path: helm/brain-ops
      ref: kernel
    - repoURL: https://github.com/your-org/your-platform.git
      targetRevision: main
      ref: values
      path: deploy/agentibrain
      helm:
        valueFiles:
          - $values/values-prod.yaml
```

Pros: kernel pinned to a tag, your overlay separate. No drift.
Cons: requires ArgoCD ≥ 2.6.

### C — `helm install` direct
For non-GitOps installs:

```bash
helm install brain-ops \
  oci://ghcr.io/the-cloud-clockwork/charts/brain-ops \
  --version 0.1.0 \
  -n <your-ops-namespace> -f values-prod.yaml
```

Pros: simplest. Cons: no GitOps trail.

## Required cluster prerequisites

| Component | Purpose |
|---|---|
| Kubernetes ≥ 1.28 | Workload plane |
| MetalLB or another LB | If you want kernel embeddings exposed on a static IP for docker consumers |
| External Secrets Operator (optional) | Bridge your secret store → K8s Secret |
| Secret store (optional) | Vault, OpenBao, AWS SM, GCP SM, Azure KV — any ESO-supported backend |
| Postgres + pgvector | Embeddings storage |
| NFS or shared PVC | Vault filesystem (single writer or RWX) |
| ArgoCD (optional) | If using GitOps |
| Reloader (optional) | Auto-restart pods on Secret/CM change |

## Platform-side state your install needs

Before pods come up, prepare:

1. **Secrets** — either via ESO (your secret store + a `ClusterSecretStore` ESO can reach) or as plain Opaque Secrets created by [`local/k8s-bootstrap.sh`](../local/k8s-bootstrap.sh). See [`SECRETS.md`](SECRETS.md).
2. **K8s Secret** `agentibrain-router-secrets` in each namespace, with the brain-api bearer token. Either kubectl-create directly or wire it into your ESO setup.
3. **NFS export or PVC** for `/vault`. Read-write from the kernel pods, read-only from agent pods if you mount the vault elsewhere.
4. **LoadBalancer IP** (optional) if you want a static IP for kernel embeddings. Set via `service.annotations.<your-LB-controller>/loadBalancerIPs` in your values overlay.

## Per-environment values overlay

Operator pattern: `values.yaml` is the dev base. `values-prod.yaml` is the prod overlay.

Example overlay (`values-prod.yaml` for agentibrain-embeddings):

```yaml
tpl:
  global:
    environment: prod
  app:
    image:
      tag: dev
  externalSecret:                        # nosecret
    awsSecretPath: <your-prefix>/embeddings  # nosecret  (path in your secret store)
  service:
    type: LoadBalancer
    annotations:
      <your-lb-controller>/loadBalancerIPs: "<your-cluster-ip>"
```

Helm merge semantics: maps merge, lists replace. If your base `values.yaml` has an `env.extra` list, your overlay's `extra:` will REPLACE it, not append. Reproduce the base list explicitly.

## Agent fleet wiring

Every agent pod that should talk to the kernel needs two env vars: `BRAIN_URL` (the brain-api service URL in-cluster) and `KB_ROUTER_TOKEN` (the bearer pulled from the K8s Secret). `agentihooks` reads both. With `BRAIN_URL` empty and the pod on K8s, agentihooks logs a critical warning.

```yaml
env:
  variables:
    BRAIN_URL: "http://agentibrain-brain-api.<ns>.svc:8080"
  extra:
  - name: KB_ROUTER_TOKEN
    valueFrom:
      secretKeyRef:                        # nosecret
        name: agentibrain-router-secrets   # nosecret
        key: KB_ROUTER_TOKEN
        optional: true
```

## Brain instance identity

Every brain names itself with `BRAIN_ID` (lowercase letters, digits and dashes,
starting with a letter; schema `schemas/brain-instance-v2.json`). A project id
such as `github.com/org/repo` is a namespace inside a brain and is never a brain
id. An installation without `BRAIN_ID` is the brain named `default`, so existing
installs keep their vault and credentials with no migration step.
`BRAIN_POLICY_REVISION` (default `1`) is served beside it.

- brain-api answers `GET /instance` without auth: `{schema, brain_id, policy_revision}`.
  It refuses to start when `BRAIN_ID` is set outside the schema pattern.
- `BrainClient(expected_brain_id=...)` calls `/instance` before any request that
  carries the bearer and raises `BrainBindingError` on a different or absent id,
  counting `brain_instance_binding_failures`. Deploy brain-api before clients
  that verify: an older brain-api has no `/instance` and is refused.
- On a compose install the identity lives only in `<config root>/.env`, which
  compose passes to brain-api and the CLI reads, so client and server read one
  value and the environment still wins. `agentibrain install` writes the two
  keys when the file lacks them and never rewrites an existing key; changing a
  brain's id or policy revision afterwards is an operator edit of that file,
  followed by a stack restart.
- Rollback: unset `BRAIN_ID` and the brain answers `default` again. Clients that
  expect another name then refuse it rather than send credentials.

### Two brains on one host

A named brain (any `BRAIN_ID` but `default`) renders its own compose project,
`agentibrain-<id>`, with no fixed container names, so its volumes and network are
project scoped. Give each one its own `AGENTIBRAIN_HOME`, vault and published ports
(`PORT_BRAIN_API`, `PORT_POSTGRES`, `PORT_REDIS`, `PORT_MCP`, `PORT_OLLAMA` in that
brain's `.env`). `agentibrain up|build|fix` re-renders the named brain's compose and
refuses to start when a port, its project name or its vault is already taken; it
never stops another stack. `down` and `uninstall` act on the selected brain only.

The default brain keeps its fixed `agentibrain_*` names and still replaces other
default stacks; it does not see named brains' ports, so such a clash surfaces as
Docker's own bind error. Rollback to a single brain: select the default brain
explicitly and leave named brains' config roots and volumes in place.

### Selecting a brain for lifecycle commands

`agentibrain --instance <id> <command>` (or `AGENTIBRAIN_INSTANCE=<id>`) acts on
one brain only. Its config root is `AGENTIBRAIN_HOME` when that root holds the
brain, otherwise `~/.agentibrain` for `default` and `~/.agentibrain-<id>` for a
named brain; a root whose `.env` names another brain is refused untouched.

- `down` and `uninstall` stop only the selected stack. Without `--instance`,
  more than one legacy stack on the host is ambiguous and refuses.
- The default brain still replaces other legacy stacks on `up`, `build`, `fix`
  and `install`, but only those with ownership evidence: a container with a
  fixed kernel name whose compose working dir holds a kernel compose file. Any
  other `agentibrain_*` container is left alone.
- `up`, `build`, `down` and `uninstall` take `--dry-run`, which prints the
  instance, its compose project and every stack it would start, stop or replace.
- `install` and `uninstall` default the agentihooks profile alias to `brain`
  for the default brain and `brain-<id>` for a named one. A rerun of an
  interrupted `install --instance <id>` completes that instance and keeps its
  bearer and identity. Only the default brain's install removes the old
  projected copy of the bearer from agentihooks; a named brain leaves that
  machine-wide file alone.
- Rollback: run `--dry-run` first and select `--instance default` explicitly.

## Image strategy

Each kernel service has its own image, tagged per branch:

| Service | Image |
|---|---|
| brain-api | `ghcr.io/the-cloud-clockwork/agentibrain-brain-api:dev` |
| embeddings | `ghcr.io/the-cloud-clockwork/agentibrain-embeddings:dev` |
| tick-engine | `ghcr.io/the-cloud-clockwork/agentibrain-brain-ops:dev` |
| mcp | `ghcr.io/the-cloud-clockwork/agentibrain-mcp:dev` |
| brain-keeper | `ghcr.io/the-cloud-clockwork/agenticore:dev` (uses agenticore base; brain-keeper is an agenticore agent, not its own image) |

Branch → tag: push to `dev` → `:dev`. ArgoCD tracks `dev`; image-updater watches the `:dev` digest. `main` is the snapshot branch — it publishes no image and nothing deploys from it.

## Smoke tests post-deploy

```bash
NS=<your-namespace>
URL=http://agentibrain-brain-api.$NS.svc:8080

kubectl -n $NS exec <agent-pod> -c agenticore -- sh -c \
  'curl -sS -o /dev/null -w "%{http_code}\n" --max-time 5 "$BRAIN_URL/feed" -H "Authorization: Bearer $KB_ROUTER_TOKEN"'
# expect 200
```

If 200, the install is wire-correct. See `OPERATIONS.md` for what to monitor next.
