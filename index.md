---
layout: home
title: Home
nav_order: 1
description: "AgentiBrain — the brain + KB substrate every Claude Code agent fleet plugs into. HTTP contract, five Helm charts, vault schema, MCP retrieval gateway."
permalink: /
---

# AgentiBrain
{: .fs-9 .fw-700 }

The brain + KB substrate for Claude Code agent fleets — services, Helm charts, agent definition, profile overlays, and the vault schema, in one self-contained kernel.
{: .fs-5 .text-grey-dk-100 .mb-6 }

<div class="hero-actions text-center mb-8" markdown="0">
  <a href="#install" class="btn btn-primary fs-5 mr-2">Get Started</a>
  <a href="{{ site.baseurl }}/concepts/" class="btn btn-green fs-5 mr-2">Concepts</a>
  <a href="https://github.com/The-Cloud-Clockwork/agentibrain-kernel" class="btn fs-5" target="_blank">View on GitHub</a>
</div>

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://github.com/The-Cloud-Clockwork/agentibrain-kernel/blob/main/LICENSE)
[![CI](https://github.com/The-Cloud-Clockwork/agentibrain-kernel/actions/workflows/ci.yml/badge.svg)](https://github.com/The-Cloud-Clockwork/agentibrain-kernel/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://python.org)
{: .text-center .mb-8 }

---

## What you get
{: .fs-7 .fw-600 }

```mermaid
flowchart LR
    Agent["Claude Code agent"] -->|HTTP| KB["kb-router\n/feed /signal /marker /tick /ingest"]
    Agent -->|MCP| MCP["agentibrain-mcp\nkb_search · kb_brief · brain_search_arcs · brain_get_arc"]
    MCP -->|/vault/search| KB
    KB --> Vault[("Obsidian vault\nleft / right hemisphere")]
    KB --> Embed["embeddings"]
    Embed --> PG[("Postgres + pgvector")]
    Tick["tick-engine"] --> CH[("ClickHouse\nbrain.tick_health")]
    Tick --> Vault
    Keeper["brain-keeper agent"] --> KB
    Keeper --> MCP
```

- **4 service images** auto-published to GHCR — `kb-router`, `embeddings`, `mcp`, `tick-engine`.
- **5 Helm charts** — `kb-router`, `embeddings`, `mcp`, `brain-cron`, `brain-keeper`.
- **Brain-keeper agent definition** in-tree (`agents/brain-keeper/`) — drop-in Claude Code custom agent.
- **Brain profile overlays** for [agentihooks](https://github.com/The-Cloud-Clockwork/agentihooks) — markers, broadcast channels, hook wiring.
- **Vault schema v1** — six-region Obsidian vault layout that the kernel writes to.
- **HTTP contract frozen at v1** — see [`api/openapi.yaml`](https://github.com/The-Cloud-Clockwork/agentibrain-kernel/blob/main/api/openapi.yaml).
- **Generic OpenAI gateway** — kernel speaks chat-completions to any compatible upstream (LiteLLM, OpenAI, Ollama, vLLM, …).

---

## Install
{: .fs-7 .fw-600 }

The kernel ships in exactly two shapes — Docker Compose for local / isolated environments, Helm for fleet-scale Kubernetes. There is no PyPI / `pip install` path; the kernel runs as containers, not as a Python library.

### 1. Local (Docker Compose)

```bash
git clone https://github.com/The-Cloud-Clockwork/agentibrain-kernel
cd agentibrain-kernel
./local/bootstrap.sh                  # mints tokens, scaffolds vault + .env
docker compose up -d                  # four services on localhost
```

See [`local/README.md`](https://github.com/The-Cloud-Clockwork/agentibrain-kernel/blob/main/local/README.md) for the laptop walkthrough.

### 2. Kubernetes (Helm)

```bash
helm dep update helm/kb-router
helm install kb-router helm/kb-router -n brain --create-namespace
# repeat for embeddings, mcp, brain-cron, brain-keeper
```

Bare-cluster path with no platform prerequisites — see [`docs/HELM-QUICKSTART.md`]({{ site.baseurl }}/docs/HELM-QUICKSTART). For ArgoCD + ESO + multi-source patterns: [`docs/DEPLOYMENT.md`]({{ site.baseurl }}/docs/DEPLOYMENT).

---

## Connect Claude Code
{: .fs-7 .fw-600 }

### Laptop — `.mcp.json`

```json
{
  "mcpServers": {
    "agentibrain": {
      "url": "http://localhost:8104/mcp",
      "headers": {
        "Authorization": "Bearer ${MCP_PROXY_API_KEY}"
      }
    }
  }
}
```

### Agent mode (in-cluster)

```json
{
  "mcpServers": {
    "agentibrain": {
      "url": "http://agentibrain-mcp.<your-namespace>.svc:8080/mcp",
      "headers": {
        "Authorization": "Bearer ${MCP_PROXY_API_KEY}"
      }
    }
  }
}
```

Full wiring + auth notes: [`docs/MCP.md`]({{ site.baseurl }}/docs/MCP).

---

## Where to next
{: .fs-7 .fw-600 }

The sidebar groups every page into four sections — pick the one that matches your goal:

- [**Concepts**]({{ site.baseurl }}/concepts/) — what the brain is, why arcs/markers/MCP, the philosophical groundwork.
- [**Reference**]({{ site.baseurl }}/reference/) — HTTP API, MCP tools, vault schema, marker grammar, gateway contract.
- [**Operate**]({{ site.baseurl }}/operate/) — Helm install, deployment patterns, secrets, day-2 ops, troubleshooting.
- [**Architecture**]({{ site.baseurl }}/architecture/) — services, data plane, the brain-keeper agent, telemetry, maturity rubric.

---

## Status

**v0.1.1 — first stable.** Five Helm charts, four service images on GHCR (`:dev` from dev branch, `:latest` from main), HTTP contract frozen at v1, brain-blind boundary in place since 2026-04-26 (artifact-store no longer auto-embeds; every embed flows through `POST /index_artifact`).

The kernel is self-contained and the canonical source of truth for everything brain-related — services, Helm charts, brain-keeper agent definition, brain profile overlays, and the vault layout schema. All deployment-specific plumbing (cluster namespaces, model name aliases, secret-store paths, NFS hosts) lives in your own platform repo, not here.
