# CLAUDE.md — agentibrain-kernel

## Identity

You are working in **agentibrain-kernel**, the standalone brain + KB substrate
for the agenti ecosystem.

## Scope

This repo owns:
- Brain services (kb-router, obsidian-reader, embeddings, tick-engine)
- Helm charts for K8s deployment (brain-keeper, brain-cron)
- The brain-keeper agent definition (single source of truth)
- Brain profile overlays for agentihooks
- The vault layout schema and the `brain scaffold` tool that writes it
- The HTTP API contract (`api/openapi.yaml`)
- The Python CLI (`brain init|up|down|status|tick|scaffold|version`)

## What this repo does NOT own

- `artifact-store` / `artifact-transform` — general storage plane, stays in antoncore
- Generic Claude Code hooks — those live in `agentihooks` (this kernel exposes HTTP, hooks talk to it)
- `broadcast.py` / `channels.py` — fleet coordination, stays in agentihooks
- Deployment values files, OpenBao secrets, operator-specific paths — stay in antoncore

## Core principle

**Two contracts, nothing else:**
1. The kernel exposes an HTTP API. No shared filesystem paths across boundaries.
2. The vault layout is a versioned schema owned by the kernel. `brain scaffold`
   is the only authoritative writer.

## Versioning

The `version` field in `pyproject.toml` is managed by the release workflow. Do
not edit it manually. The runtime version lives in `agentibrain/__init__.py`
(`__version__`).

## Working model

Normal dev-first flow:
- Feature work on a feature branch → PR to `main` → operator merges.
- `main` is the canonical trunk (this repo doesn't run a separate `dev` branch yet).
- Image builds go to `ghcr.io/the-cloud-clock-work/agentibrain-*`.

## Downstream consumers

- `antoncore` — operator's live deployment. Uses the kernel's Helm charts with operator-specific values.
- `agentihub` — clones `agents/brain-keeper/` at install time.
- `agentihooks-bundle` — clones `profiles/brain/` and `profiles/brain-keeper/` at install time.
- Friends / external users — `pip install agentibrain` → `brain init --local`.
