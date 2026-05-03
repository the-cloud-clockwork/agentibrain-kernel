---
id: INC-2026-05-03-block2-prod-smoke
title: Block 2 prod cutover smoke + brain-cron singleton fix
date: 2026-05-03
status: closed
---

# Block 2 prod cutover smoke (2026-05-03)

## Context

Block 2 was de-facto already shipped in prod (overlays + ArgoCD apps on antoncore main, prod brain pods Running 29h–45h). Two open items remained:

1. `agentibrain-brain-cron-prod` `OutOfSync` with `SharedResourceWarning` — both dev + prod ArgoCD Applications targeted `anton-ops` with identical resource names.
2. Prod smoke + 24h observation never executed.

## Singleton fix

brain-cron is a singleton (one CronJob set per cluster, one operator vault). Resolution: delete the prod-tracking ArgoCD Application; keep `agentibrain-brain-cron` (dev-tracking) — internal infra benefits from faster code rollout.

PR: `antoncore` `chore/block2-close-prod-cutover` (deletes `k8s/argocd/prod/agentibrain/agentibrain-brain-cron.yaml`).

## Smoke matrix

Run from `agentibrain-kb-router-0` in `anton-prod`:

```
=== GET /feed ===
hot_arcs populated, entry_count > 0, hot-arcs-2026-05-03 present

=== GET /signal ===
{"active":false, ...}  ← amygdala empty, expected

=== POST /marker (1st) ===
{"ok":true,"idempotency_key":"block2-smoke-1777832937",
 "vault_path":"left/reference/lessons-2026-05-03.md",
 "action":"appended","marker_type":"lesson","written_bytes":85}

=== POST /marker (replay, same idempotency key) ===
{"ok":true, "idempotent_replay":true, ...}

=== POST /ingest ===
{"batch_id":"b7bcbd38697e",
 "obsidian_path":"raw/inbox/2026-05-03-block-2-prod-cutover-smoke-test-2026-05-03.md",
 "errors":[]}
```

All four endpoints 2xx. Idempotency replay verified. Ingest landed in vault.

## Block 2 status

Closed for 2A/2B/2C/2D items. 24h observation runs in parallel — kb-router-prod / brain-keeper-prod log error counts to be re-checked tomorrow.
