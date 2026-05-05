---
title: Glossary
parent: Concepts
nav_order: 3
---

# Glossary

Terms used across the codebase, docs, and operator vocabulary.

## Brain shape

**Vault** — the on-disk Obsidian-compatible markdown directory. Single source of truth for memory, knowledge, and signals. Mounted into kernel pods at `/vault`.

**Hemisphere** — top-level vault division.
- **Left** — technical, systematic, project-bound (`left/projects/`, `left/research/`, `left/decisions/`, `left/incidents/`, `left/reference/`).
- **Right** — creative, strategic, life (`right/ideas/`, `right/strategy/`, `right/life/`, `right/creative/`, `right/risk/`).
- **Bridge** — cross-hemisphere synthesis (`bridge/vision.md`, `bridge/connections.md`).

**Region** — non-hemisphere vault directory with cognitive role:
- `raw/` — ingest staging
- `clusters/` — canonical arc storage
- `brain-feed/` — hot feed (read by `/feed`, written by tick)
- `amygdala/` — emergency signals
- `frontal-lobe/` — working memory (`conscious/`, `unconscious/`)
- `pineal/` — joy, breakthrough notes
- `daily/` — append-only daily logs
- `identity/` — root node (about-me, goals, principles, stack)

## Memory primitives

**Arc** — a cluster of related session activity, materialised as a markdown file under `clusters/<date>/<id>-<author>.md`. Created by the tick-engine from session transcripts.

**Hot arc** — an arc whose `heat` is above threshold; surfaced in `brain-feed/hot-arcs.md` and read on every agent SessionStart via `/feed`.

**Heat** — integer score on an arc (0–10). Bumped on access, decayed by age. Drives promotion/demotion.

**Promotion** — when an arc's heat crosses threshold, it joins `hot-arcs.md`. Demotion is the reverse.

**Graduation** — when a hot arc is older than the retention window, it moves to `frontal-lobe/unconscious/` and stops appearing in feed.

**Marker** — an HTML-comment annotation emitted by an agent in its turn output. Four types:
- `lesson` → `left/reference/lessons-YYYY-MM-DD.md` (append)
- `milestone` → project's `BLOCKS.md` if known, else `daily/YYYY-MM-DD.md` (append)
- `signal` → `amygdala/<timestamp>-<severity>-<slug>.md` (new file)
- `decision` → `left/decisions/ADR-NNNN-<slug>.md` (auto-numbered)

Full grammar: `architecture/MARKERS.md`.

**Signal** — emergency state. Severity: `nuclear | critical | warning | info | resolved`. Active signal lives at `amygdala/amygdala-active.md` and is broadcast cluster-wide.

**Tick** — periodic cycle that maintains the brain. Two modes:
- **Scheduled tick** (`brain-cron` CronJob, every 2 h): full hybrid pass — deterministic clustering + LLM reasoning + amygdala check.
- **On-demand tick** (`/tick` endpoint): drained by `tick-drain` CronJob (every 2 min); processes one request at a time.

**MUBS** — Minimal Unit of Brain Storage. The standard set of project files: `VISION`, `SPECS`, `BLOCKS`, `TODO`, `STATE`, `BUGS`, `KNOWN-ISSUES`, `ENHANCEMENTS`, `MVP`, `PATCHES`. Templates in `templates/mubs/`.

## Services

**kb-router** — HTTP entrypoint. Implements `/feed /signal /marker /tick /ingest`. Bearer auth.

**obsidian-reader** — read-only vault access (list, read, search) + bounded write-inbox.

**embeddings** — pgvector wrapper. `/embed` generates a vector via LiteLLM, `/search` does similarity search.

**tick-engine** — runs the periodic and on-demand ticks. Bundled scripts: `extract.py`, `cluster.py`, `brain_keeper.py`, `brain_tick.py`, `amygdala.py`.

**brain-keeper** — first-class agent for brain operations (triage, enrichment, replay). Same image as agenticore agents, different env vars.

## Agents-side

**Agent fleet** — pods running `agenticore` image with `BRAIN_URL=http://agentibrain-kb-router.<ns>.svc:8080` set; agentihooks talks to the kernel over HTTP.

**SessionStart** — agentihooks lifecycle event; `brain_adapter` hook calls `/feed` and injects hot arcs into the agent's first turn.

**Stop hook** — agentihooks lifecycle event; `brain_writer_hook` parses session output for markers and POSTs them via `/marker`.

**Idempotency key** — `X-Idempotency-Key` header on `/marker`; replays return the original response with `idempotent_replay: true`. TTL configurable (default 1 h).

## Storage

**ESO** — [External Secrets Operator](https://external-secrets.io/). Bridges secrets from your store of choice (Vault, OpenBao, AWS SM, GCP SM, Azure KV, …) into K8s `Secret` resources.

**ClusterSecretStore** — K8s CR pointing ESO at your secret store with the auth credentials it needs.

**ExternalSecret** — K8s CR declaring "pull these keys from this store path → write to this K8s Secret". Created by the kernel chart when `externalSecret.enabled: true`.

**Secret-store path** — wherever the embeddings credentials and other env vars live in your secret store. The kernel doesn't dictate a convention; you pass the path via `externalSecret.awsSecretPath`.

## Operations

**Parity** — the dual-write window during a brain swap. Both legacy and kernel get writes; a parity CronJob compares.

**Drain** — `kubectl rollout`-style restart, or, in tick context: emptying `brain-feed/ticks/requested/`.

**Anchor svc** — a legacy-named K8s Service whose selector points at kernel pods, kept alive to avoid breaking external consumers (e.g. docker stacks bound to a static IP).

## Environment

**dev** — your dev namespace, image tag `:dev`, ArgoCD source branch `dev`, secret-store paths suffixed `-dev`.

**prod** — your prod namespace, image tag `:latest`, ArgoCD source branch `main`, no suffix.
