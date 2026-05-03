# examples/

Sample value overlays + ArgoCD `Application` CRs to deploy `agentibrain-kernel`
into your own cluster. Copy these into your platform repo, replace every
`<your-*>` placeholder, then either `helm install` directly or point ArgoCD at
your repo.

## Layout

```
examples/
├── values-overlays/        ← per-chart values overlays (operator-side)
│   ├── kb-router/{values-dev,values-prod}.yaml.example
│   ├── embeddings/{values-dev,values-prod}.yaml.example
│   ├── obsidian-reader/{values-dev,values-prod}.yaml.example
│   ├── brain-keeper/values-prod.yaml.example
│   └── brain-cron/values.yaml.example          (singleton — one per cluster)
│
└── argocd/                 ← ArgoCD Application CRs (operator-side)
    ├── agentibrain-root.yaml.example           (app-of-apps, picks one env)
    ├── dev/agentibrain-{kb-router,embeddings,obsidian-reader,brain-keeper,brain-cron,mcp}.yaml.example
    └── prod/agentibrain-{kb-router,embeddings,obsidian-reader,brain-keeper,mcp}.yaml.example
                                                 (no brain-cron-prod — singleton)
```

## Placeholder vocabulary

| Placeholder | What it is |
|---|---|
| `<your-platform-repo>` | Your operator-owned platform repo URL (where you copy `examples/` into) |
| `<your-revision>` | Branch / tag your platform repo tracks (`main`, `dev`, `v1.0`) |
| `<your-namespace>` | K8s namespace for prod brain pods (e.g. `brain-prod`) |
| `<your-dev-namespace>` | K8s namespace for dev brain pods (e.g. `brain-dev`) |
| `<your-ops-namespace>` | K8s namespace for cluster-singleton workloads like brain-cron (e.g. `brain-ops`) |
| `<your-llm-gateway>` | Hostname of an OpenAI-compatible LLM gateway (LiteLLM, OpenAI, Ollama). See `docs/GATEWAY-CONTRACT.md`. |
| `<your-classify-model>` | Model name your gateway resolves for `BRAIN_CLASSIFY_MODEL` |
| `<your-brief-model>` | Model name your gateway resolves for `BRAIN_BRIEF_MODEL` |
| `<your-secret-store>` | ESO `ClusterSecretStore` name pointing at your secret manager (OpenBao, AWS SM, Vault, Akeyless) |
| `<your-secret-path>` | Path inside your secret store holding `KB_ROUTER_TOKEN`, `INFERENCE_API_KEY`, etc. |
| `<your-host>` | NFS server or PVC host for the vault mount |
| `<your-vault-path>` | NFS export path or PVC claim name for the operator vault |

## How to use

1. Copy `examples/` into your platform repo (e.g. `your-platform/k8s/`).
2. `find examples/ -type f -exec sed -i 's/<your-namespace>/brain-prod/g' {} \;` etc — replace every placeholder with your real values.
3. Drop the `.example` suffix.
4. `kubectl apply -f your-platform/k8s/argocd/agentibrain-root.yaml` — ArgoCD picks up the rest.

## Singletons vs per-env

- **kb-router, embeddings, obsidian-reader, brain-keeper** — deploy one per env (`-dev`, `-prod`).
- **brain-cron, mcp-agentibrain** — singletons. One CronJob set + one MCP server per cluster, since they operate on a single operator vault.

## See also

- `docs/DEPLOYMENT.md` — generic deployment guide
- `docs/SECRETS.md` — ESO + secret-store contract
- `docs/ENVIRONMENTS.md` — operator-reference walk-through (concrete example)
- `docs/GATEWAY-CONTRACT.md` — LLM gateway wiring options
