# Brain MCP server

The kernel ships an MCP server — `agentibrain-mcp` — that exposes brain + KB retrieval tools to agents. It lives in `services/mcp/` and is the canonical retrieval surface. Agents do not query Postgres, Obsidian, or the inference-gateway directly; they call these tools.

## Tools (Phase 1)

| Tool | Purpose |
|---|---|
| `brain_search_arcs` | Semantic search over arcs in `clusters/`. Filters by `min_heat` and `min_score`, ranked by cosine similarity. |
| `brain_get_arc` | Fetch full text + metadata for one arc by `cluster_id`. |
| `kb_search` | Federated retrieval across embeddings (semantic) + obsidian-reader (text). Normalised, score-ranked. |
| `kb_brief` | Runs `kb_search`, synthesises a 3-5 line brief via inference-gateway, returns `candidate_refs` ready to feed downstream. |

Tool source: `services/mcp/app/tools/{arcs.py,kb.py}`.

`kb_dispatch` and `kb_converse` are Phase 2 — they need bundle + theme assembler packages that currently live in the upstream `artifact-store` MCP.

## Image

`ghcr.io/the-cloud-clock-work/agentibrain-mcp:dev|latest`

Built from `services/mcp/Dockerfile` by the kernel `docker-build` workflow on every push to `dev` (→`:dev`) and `main` (→`:latest`).

## Runtime

Dockerfile pattern: FastMCP stdio Python server wrapped by `mcp-proxy` (npm) for HTTP/SSE. Port 8080. `/ping` returns `pong`. `/mcp` is the JSON-RPC endpoint.

Required env vars:

| Var | Purpose |
|---|---|
| `EMBEDDINGS_URL` | agentibrain-embeddings service base URL |
| `EMBEDDINGS_API_KEY` | bearer for embeddings — same token the kernel-internal services use |
| `OBSIDIAN_READER_URL` | agentibrain-obsidian-reader service base URL |
| `OBSIDIAN_READER_TOKEN` | bearer for obsidian-reader |
| `INFERENCE_URL` | inference-gateway endpoint (used by `kb_brief`) |
| `KB_BRIEF_ROUTE` | named route in inference-gateway config |
| `MCP_PROXY_API_KEY` | bearer enforced by mcp-proxy on inbound requests |

Auth is two-layered:
1. The proxy authenticates the caller (LiteLLM gateway) with `MCP_PROXY_API_KEY`.
2. The Python tools authenticate themselves to embeddings + obsidian-reader with their own bearer tokens.

## Install — Kubernetes

The kernel does not ship a Helm chart for the MCP. Operators deploy it using whatever generic MCP-proxy chart they have.

Reference deploy (in your platform repo, e.g. `k8s/argocd/{dev,prod}/mcp-agentibrain.yaml`):

```yaml
spec:
  source:
    path: k8s/charts/mcp-proxy        # your generic mcp-proxy chart
    helm:
      valuesObject:
        tpl:
          fullnameOverride: mcp-agentibrain
          env:
            variables:
              EMBEDDINGS_URL: http://agentibrain-embeddings.<ns>.svc:8080
              OBSIDIAN_READER_URL: http://agentibrain-obsidian-reader.<ns>.svc:8080
              INFERENCE_URL: http://<inference-gateway-ip>:8103
              KB_BRIEF_ROUTE: kb-brief
          app:
            image:
              repository: ghcr.io/the-cloud-clock-work/agentibrain-mcp
              tag: dev   # or latest
          secrets:
            external:
              secretName: mcps-secrets   # supplies EMBEDDINGS_API_KEY, OBSIDIAN_READER_TOKEN, MCP_PROXY_API_KEY
```

Pod listens on port 8080. Expose as `ClusterIP` for in-cluster consumers (e.g. LiteLLM gateway).

## Wiring through LiteLLM

Register `agentibrain` as an MCP server pointing at the in-cluster service URL. Bind it to the relevant unit (e.g. `tools-knowledge`) so agents see the tools as `mcp__<unit>__agentibrain-<tool>`.

GitOps path (in your `litellm-state` repo):
1. `servers/agentibrain.json` — server entry with `url: http://mcp-agentibrain.<ns>:8080/mcp`, `transport: http`, `auth_type: api_key`.
2. `teams/<team>.json` — add `agentibrain` to the team's server allowlist.
3. `units/<unit>.json` — add `agentibrain` under `servers` and list the 4 tool names under `tools.agentibrain`.

Reconcile workflow handles the rest. After reconcile, restart `litellm-0` once so it refreshes its in-process MCP tool cache.

## Local dev

```bash
cd services/mcp
pip install -r requirements.txt
EMBEDDINGS_URL=http://localhost:18080 \
EMBEDDINGS_API_KEY=... \
OBSIDIAN_READER_URL=http://localhost:18101 \
python app/server.py    # stdio MCP — connect with mcp-proxy or claude --debug
```

Smoke tests: `pytest services/mcp/tests/`.

## Phase 2 — likely deprecated

`kb_dispatch` and `kb_converse` build a `JobBundle` (theme + focus + custom + audience + sources) and dispatch it to mediagen / notebooklm / paper2slides via an upstream job-runner. The bundle assembler and theme registry live in upstream packages (~700 LOC + ~260 LOC).

**Status (2026-04-26):** these tools were made **brain-blind** in artifact-store:
they reject `obsidian://` source refs and never call `kb_brief`. The clean
boundary now justifies leaving them in artifact-store permanently — brain owns
retrieval (`kb_search` / `kb_brief` / `brain_*` here), artifact-store owns
artifact CRUD and media dispatch. Phase 2 (vendoring the bundle/theme packages
into the kernel and moving the dispatch tools here) is no longer
required by the boundary contract; it would only consolidate code, not change
responsibilities.
