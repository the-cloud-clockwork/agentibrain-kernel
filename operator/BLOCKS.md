---
id: agentibrain-kernel-blocks
title: agentibrain-kernel — Active Blocks
project: agentibrain-kernel
status: active
updated: 2026-04-22
---

# agentibrain-kernel — Active Blocks

Tackle one block at a time, top to bottom. Each checkbox has an acceptance criterion — don't tick without it.

---

## Block 1 — Dev cutover close-out (Tier 1, in-flight)

**Status:** in progress. Streams 1/2/3/4-prework/4A on dev branch, dev→main PRs not yet opened.
**Gate to next block:** all 12 items ticked, 24h parity green, `v0.1.0` tag published to PyPI.

### 1A — Agent wiring (blocks validation) ✅ DONE 2026-04-24
- [x] Wire `BRAIN_URL=http://agentibrain-kb-router.anton-dev.svc:8080` + `KB_ROUTER_TOKEN` into 5 dev agent charts: agenticore, publisher, finops-agent, anton-agent, diagram-agent. (Legacy brain-keeper skipped — kernel `agentibrain-brain-keeper` replaces it in 1E.)
      **Accepted:** all 5 pods carry BRAIN_URL + secretKeyRef-bound KB_ROUTER_TOKEN. antoncore commit `fac3dc18`.
- [x] `agentibrain-router-secrets` exists in anton-dev with `KB_ROUTER_TOKEN` populated.
      **Accepted:** `kubectl get secret agentibrain-router-secrets -o jsonpath='{.data.KB_ROUTER_TOKEN}' | base64 -d` returns 32-char token.

### 1B — Validation (blocks PR)
- [x] Re-smoke `/feed /signal /marker /tick` from inside an actual agent pod (not host shell). ✅ 2026-04-24
      **Accepted:** from `agenticore-0`: /feed 200 (5 entries, hash `c4d87ac3f961be48`), /signal 200 (inactive), /marker 201 (appended to `left/reference/lessons-2026-04-24.md` +1394 bytes), /tick 202 (job_id `5730f85dff57` enqueued).
- [x] End-to-end: marker emitted via `/marker` from pod lands in vault. ✅ 2026-04-24
      **Accepted:** `lessons-2026-04-24.md` tail shows the block1b-smoke entry on NFS.
- [x] 24h parity green. ✅ 2026-04-24
      **Accepted:** `agentibrain-parity` CronJob up 45h+ in anton-dev, hourly fire at minute 17, last 3 visible runs (k8s GC retains 3) all exit=0 with `endpoint=embeddings-health status=green detail='200'`. Last successful: 2026-04-24T13:17:51Z.
- [x] Tick-engine on-demand consumer shipped. ✅ 2026-04-24
      **Accepted:** new `tick-drain` CronJob in helm/brain-cron fires every 2 min, loops `brain-feed/ticks/requested/*.json`, invokes `brain_tick.py` per file, moves to `completed/` or `failed/`. Gated by `tickDrain.enabled` (default true). Mirrored into antoncore k8s/charts/agentibrain-brain-cron (antoncore commit `d445039d`).

### 1C — Stream 4B+C (blocks legacy tear-down) ✅ DONE 2026-04-24
- [x] Stream 4B — legacy `anton-embeddings` dev StatefulSet scaled to 0.
      **Accepted:** `kubectl get sts anton-embeddings -n anton-dev` shows `0/1`.
- [x] Stream 4C — 3 legacy ArgoCD dev apps deleted with cascade=foreground: `anton-embeddings`, `brain-cron`, `brain-keeper`. Antoncore chart dirs removed: `k8s/charts/{anton-embeddings,brain-cron,brain-keeper}/`. App YAMLs removed: `k8s/argocd/dev/{anton-embeddings,brain-cron,brain-keeper}.yaml`. antoncore commit `d445039d`.
      **Accepted:** `kubectl -n argocd get app brain-cron brain-keeper anton-embeddings` returns no entries post-cascade. No consumers found pre-delete.

### 1D — PR + publish (closes Block 1)
- [ ] Open dev→main PR on `agentibrain-kernel` with README + `operator/` + Streams 1-4 + `v0.1.0` version bump.
      **Accept:** PR URL in operator's hands; CI green.
- [ ] Open 4 downstream dev→main PRs after kernel merges (agentihooks, agentihub, agentihooks-bundle, antoncore) bumping kernel pin.
      **Accept:** 4 PR URLs listed in BLOCKS.md with "open" status.
- [ ] Drift-check CI job on downstream repos fails loud on stale vendored copies.
      **Accept:** temporarily mutate a vendored file in a throwaway branch → CI goes red with `kernel_drift_detected`.
- [ ] Cut `v0.1.0` tag on `agentibrain-kernel/main` → confirm `publish.yml` fires → verify `agentibrain==0.1.0` on PyPI.
      **Accept:** `pip index versions agentibrain` shows `0.1.0`.

### 1E — Post-merge cleanup (closes dev swap)
- [ ] Delete legacy chart directories from antoncore: `k8s/charts/anton-kb-router`, `anton-obsidian-reader`, `anton-embeddings`, `anton-tick-engine`.
      **Accept:** `ls k8s/charts/ | grep -c '^anton-.*\(router\|reader\|embeddings\|tick\)'` returns `0`.
- [ ] ArgoCD prunes 4 legacy apps cleanly.
      **Accept:** `kubectl get sts,svc -n anton-dev | grep anton-kb-router` returns empty. No orphans.

---

## Block 2 — Prod cutover (Tier 2, queued)

**Status:** not started. Depends on Block 1 complete + dev soak ≥48h.
**Gate to next block:** prod smoke green, legacy prod StatefulSets scaled to 0, 24h prod observation clean.

### 2A — Prod storage + secrets
- [ ] `values-dev.yaml` / `values-prod.yaml` split on all 5 kernel charts.
      **Accept:** both files exist in `helm/charts/*/` with distinct image tags (`:dev` vs `:latest`).
- [ ] OpenBao `secret/k8s/agentibrain-*-prod` paths populated (embeddings, kb-router, obsidian-reader, tick-engine, agent-env).
      **Accept:** `vault kv list secret/k8s/ | grep agentibrain.*-prod` shows 5 paths.
- [ ] ESO ExternalSecrets synced in `anton-prod`.
      **Accept:** `kubectl get externalsecret -n anton-prod | grep agentibrain` all show `SecretSynced=True`.

### 2B — Prod deploy
- [ ] Prod ArgoCD apps created tracking `:latest` — `agentibrain-embeddings`, `agentibrain-kb-router`, `agentibrain-obsidian-reader`, `agentibrain-tick-engine`, `agentibrain-brain-keeper`.
      **Accept:** `argocd app list | grep agentibrain.*-prod` shows 5 apps, all `Synced+Healthy`.
- [ ] 5 pods running in `anton-prod`.
      **Accept:** `kubectl get pods -n anton-prod | grep agentibrain | grep -c Running` == 5.

### 2C — Client cutover
- [ ] Flip `EMBEDDINGS_URL` in prod `mcp-artifact-store.yaml` + Docker `stacks/artifact-store/compose.yml` to kernel service.
      **Accept:** artifact-store embedding writes land in kernel pgvector, confirmed via `SELECT count(*) FROM content_embeddings WHERE producer='…' AND created_at > now() - interval '5 minutes'`.
- [ ] Prod agents `BRAIN_URL` flip — same wiring as 1A but prod namespace.
      **Accept:** `kubectl describe pod <agent>-0 -n anton-prod | grep BRAIN_URL` shows prod kernel URL on all prod agents.
- [ ] Service aliases so `anton-kb-router.anton-prod.svc` resolves to new kernel Service (zero-downtime for clients that haven't migrated).
      **Accept:** `kubectl get svc anton-kb-router -n anton-prod -o jsonpath='{.spec.selector}'` points at agentibrain labels.

### 2D — Prod smoke + tear-down
- [ ] Prod smoke — `/feed /signal /marker /tick /ingest` all green from a prod agent pod.
      **Accept:** same curl matrix as 1B but `-n anton-prod`.
- [ ] 24h prod observation — no error spike in Grafana `brain-health` dashboard.
      **Accept:** Grafana panel screenshots attached to `operator/incidents/` (or green check in BLOCKS.md).
- [ ] Scale legacy `anton-embeddings` prod StatefulSet to 0.
      **Accept:** `kubectl get sts anton-embeddings -n anton-prod -o jsonpath='{.spec.replicas}' == 0`.
- [ ] Delete prod legacy ArgoCD apps + chart dirs (mirror of 1C/1E in prod).
      **Accept:** `argocd app list | grep '^anton-.*-prod$' | grep -E '(router|reader|embedding|tick)'` returns empty.

---

## Block 3 — Friend-install story (Tier 2, queued)

**Status:** not started. Depends on `v0.1.0` on PyPI (Block 1D).
**Gate to next block:** a friend (or operator on a clean machine) can `pip install agentibrain && brain init --local && brain up && brain scaffold` and get a working vault + 4 healthy services in under 10 minutes, without consulting the author.

### 3A — Install smoke on clean machine
- [ ] `pip install agentibrain==0.1.0` on a fresh Python 3.11+ venv — installs without errors.
      **Accept:** `pip show agentibrain` on a machine that isn't this one.
- [ ] `brain init --local --vault /tmp/test-vault --openai-key $OPENAI_API_KEY` writes valid `~/.agentibrain/config.yaml` + `.env` (chmod 600) + `compose.yml`.
      **Accept:** all 3 files exist, `.env` perms `600`, config.yaml parses.
- [ ] `brain up` brings all 4 services + Postgres + Redis + MinIO up.
      **Accept:** `docker compose ps --filter status=running | wc -l` ≥ 7 (4 services + 3 storage).
- [ ] `brain scaffold` against an empty vault seeds 30 folders + 52 files without error.
      **Accept:** `find /tmp/test-vault -type d | wc -l` ≥ 30; `find /tmp/test-vault -type f -name '*.md' | wc -l` ≥ 52; `.brain-schema` exists.
- [ ] `brain status` reports all services healthy.
      **Accept:** exit code 0, output shows 4× `OK`.

### 3B — Ergonomics polish (from smoke findings)
- [ ] Any rough edges found in 3A → README patch + changelog entry in `CHANGELOG.md`.
      **Accept:** README regenerated, commit `docs(readme): incorporate v0.1.0 install feedback`.
- [ ] Error messages actionable (`brain init` fails cleanly when docker daemon isn't running, when port 8080 is in use, when vault path has no write perms).
      **Accept:** 3 negative-path tests in `tests/e2e/test_friend_install.py` all green.
- [ ] Uninstall path documented — `brain down && brain purge` works, purge prompts for confirmation.
      **Accept:** `brain purge` removes `~/.agentibrain/` + associated docker volumes.

### 3C — Docs pass
- [ ] Screencast or asciinema of the 5-minute local install (optional but high-signal).
- [ ] FAQ section in README for top 5 install gotchas surfaced during 3A.
- [ ] `CONTRIBUTING.md` — how to run tests, what the PR bar is.

---

## Block 4 — Tier 3 hardening (backlog, not scheduled)

See `operator/ENHANCEMENTS.md` for the full Tier 3-5 list. Pull from there only after Blocks 1-3 are green.
