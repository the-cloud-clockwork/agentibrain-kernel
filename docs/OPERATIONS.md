---
title: Operations
parent: Operate
nav_order: 5
---

# Operations — Day 2

What the kernel needs from you once it's running. Everything here assumes the install is complete (see `INSTALL.md` and `DEPLOYMENT.md`).

## Health at a glance

```
Component               How to check                                         Frequency
─────────────────────────────────────────────────────────────────────────────────
agentibrain-kb-router   GET $BRAIN_URL/feed (HTTP 200 + entry_count > 0)     30 s parity
agentibrain-embeddings  GET <lb>/health (HTTP 200)                           30 s parity
agentibrain-obsidian-r  pod Ready, no restart loop                            visual
brain-keeper            pod Ready, no restart loop                            visual
tick-engine cron        last successful job in 2h+15min window                visual
tick-drain cron         last 5 jobs Completed=1                               visual
amygdala-active.md      not present (or signal acknowledged)                  visual
embeddings-secrets ES   Ready=True, last sync < 60s ago                       visual
```

The shipped `agentibrain-parity` CronJob does the kb-router + embeddings probe every hour at minute 17 and writes a marker into `left/reference/lessons-YYYY-MM-DD.md`. Tail that file to see live state.

## Routine commands

```bash
# pod overview
kubectl -n <your-namespace> get pod -l 'app.kubernetes.io/instance in (agentibrain-kb-router,agentibrain-embeddings,agentibrain-obsidian-reader,agentibrain-brain-keeper)'

# tick history (last 10 fires)
kubectl -n <your-ops-namespace> get jobs | grep agentibrain-brain-cron | tail -10

# tick-drain history
kubectl -n <your-ops-namespace> get jobs | grep agentibrain-brain-cron-tick-drain | tail -10

# pending tick queue
ssh <your-vault-host> "ls <your-vault-path>/brain-feed/ticks/requested/ 2>/dev/null | wc -l"

# kb-router request volume (last 200 log lines)
kubectl -n <your-namespace> logs agentibrain-kb-router-0 --tail=200 | grep -c "POST\|GET"
```

## Restart / rollout

```bash
# kb-router
kubectl -n <your-namespace> rollout restart sts/agentibrain-kb-router

# all kernel pods in a namespace
for sts in agentibrain-kb-router agentibrain-embeddings agentibrain-obsidian-reader agentibrain-brain-keeper; do
  kubectl -n <your-namespace> rollout restart sts/$sts
done

# tick-engine cron (force a run NOW instead of waiting for cadence)
kubectl -n <your-ops-namespace> create job --from=cronjob/agentibrain-brain-cron \
  agentibrain-brain-cron-manual-$(date +%s)
```

## Scaling

The kernel services are stateless to the K8s scheduler — their state is the vault NFS mount + the embeddings Postgres. You can scale `agentibrain-kb-router` horizontally:

```bash
kubectl -n <your-namespace> scale sts/agentibrain-kb-router --replicas=3
```

`obsidian-reader` and `embeddings` similarly. `tick-engine` is a CronJob, single-fire.

## Backup

The vault is the canonical KB. Three storage planes need backup:

1. **Vault NFS** — your vault root path. Snapshot daily (filesystem snapshot, `rsync`, or restic to a second target).
2. **Postgres embeddings DB** — `pg_dump` schedule. The embeddings table is the only schema-bearing data.
3. **Your secret store** — back it up via whatever snapshot/export mechanism your store provides (Vault snapshot API, AWS SM versioning, etc.). The kernel doesn't manage secret storage.

The kernel itself stores nothing of value beyond what's in those three. Pods can be rebuilt from images; CRs can be rebuilt from helm templates.

## Cadence

| Tick / Job | Schedule | What it does |
|---|---|---|
| `agentibrain-brain-cron` | `7 */2 * * *` (every 2 h) | Hybrid tick: extract → cluster → reason → apply → verify |
| `agentibrain-brain-cron-tick-drain` | `*/2 * * * *` (every 2 min) | Drain `/tick` requests from `brain-feed/ticks/requested/` |
| `agentibrain-parity` | `17 * * * *` (every hour) | Smoke probe kb-router + embeddings, write marker |
| ESO sync | `30s` refresh interval | Pull from your secret store → K8s Secret |

## Capacity

- kb-router: ~100 req/s per pod with default resources (300m CPU, 768Mi mem). Scale horizontally.
- embeddings: ~20 embedding/s per pod (bound by LiteLLM). Scale via `replicaCount` only if the LLM provider can keep up.
- vault NFS: read-heavy. Many concurrent agent reads of `brain-feed/*` are cheap. Single-writer for tick output.

## Logs that matter

```bash
# critical fallback warning (agentihooks emits this when BRAIN_URL is unset on a K8s pod)
kubectl -n <your-namespace> logs <agent-pod> | grep brain_http_disabled_in_k8s

# ESO sync errors
kubectl -n external-secrets logs deploy/external-secrets | grep -i "secretsync\|error"

# kb-router 4xx/5xx
kubectl -n <your-namespace> logs agentibrain-kb-router-0 --tail=500 | grep -E "HTTP/1.1\" [45]"
```

## Drain mode

To stop accepting traffic on a single kb-router pod (e.g. for upgrade):

```bash
kubectl -n <your-namespace> label pod agentibrain-kb-router-0 ready=false --overwrite
# then patch the Service selector to add ready=true to keep traffic off this pod
# … after upgrade …
kubectl -n <your-namespace> label pod agentibrain-kb-router-0 ready- --overwrite
```

For full kernel drain (all services): scale all sts to 0, wait, scale back. Vault NFS stays intact.

## Upgrade procedure

1. Update tag in chart `image.tag` (or let ArgoCD image-updater do it).
2. Watch rollout: `kubectl -n <your-namespace> rollout status sts/agentibrain-<svc>`.
3. Smoke `/feed` from inside an agent pod.
4. Watch `agentibrain-parity` next fire — should still be green.

If smoke fails: `kubectl rollout undo sts/agentibrain-<svc>`.

## When something breaks

→ `TROUBLESHOOTING.md`.
