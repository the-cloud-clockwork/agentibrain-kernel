---
id: agentibrain-kernel-todo
title: agentibrain-kernel — TODO
project: agentibrain-kernel
status: active
updated: 2026-04-22
---

# agentibrain-kernel — TODO (next actions)

## Now (can execute today)

1. **Open 5 dev→main PRs** — needs operator PR signal. One sentence like "open the PRs" unblocks this.
2. **Wire `BRAIN_URL` into dev agent charts** — 5 charts (agenticore, publisher, finops-agent, anton-agent, diagram-agent). `BRAIN_URL=http://agentibrain-kb-router.anton-dev.svc:8080` + `BRAIN_HTTP_TOKEN` from `agentibrain-router-secrets`. Without this the hooks still hit the filesystem path and nothing really uses the kernel.
3. **Tick-engine consumer** — either a new daemon in `services/tick-engine/` that watches `brain-feed/ticks/requested/`, or fold the watch into the existing CronJob's entrypoint. Close the `/tick` loop.

## Soon (waiting on parity or gates)

4. **24h parity green check** — look at the CronJob logs at hour +24 (2026-04-23 ~19:00 UTC): `kubectl -n anton-dev logs -l job-name --tail=200 --prefix`.
5. **Stream 4B+C retire** — after parity green, scale `anton-embeddings` dev to 0, delete legacy ArgoCD apps + charts + stacks/ dirs.
6. **E2E validation** — dispatch a dev agent, read its transcript, confirm it quoted a hot_arc from `/feed` in its context.

## Later (Tier 2+)

See `ENHANCEMENTS.md`.

## Quick-reference commands

```bash
# Check parity CronJob last run
KUBECONFIG=~/.kube/config-k3s kubectl -n anton-dev logs -l app.kubernetes.io/name=agentibrain-parity --tail=100

# Kick a manual parity run
KUBECONFIG=~/.kube/config-k3s kubectl -n anton-dev create job --from=cronjob/agentibrain-parity parity-manual-$(date +%s)

# Smoke /feed against live kb-router
KUBECONFIG=~/.kube/config-k3s kubectl -n anton-dev exec agentibrain-kb-router-0 -- \
  python3 -c "import os,urllib.request,json; r=urllib.request.Request('http://localhost:8080/feed', headers={'Authorization':'Bearer '+os.environ['KB_ROUTER_TOKEN']}); print(json.dumps(json.loads(urllib.request.urlopen(r).read()), indent=2)[:500])"

# Check v0.1.0 tag + publish status
gh api repos/The-Cloud-Clock-Work/agentibrain-kernel/releases/tags/v0.1.0 2>/dev/null | head -20
pip index versions agentibrain  # or curl pypi.org/pypi/agentibrain/json
```
