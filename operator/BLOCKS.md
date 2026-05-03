---
id: agentibrain-kernel-blocks
title: agentibrain-kernel — Active Blocks
project: agentibrain-kernel
status: active
updated: 2026-04-30
---

# agentibrain-kernel — Active Blocks

Tackle one block at a time, top to bottom. Each checkbox has an acceptance criterion — don't tick without it.

---

## Block 1 — Dev→main (closed 2026-05-03)

**Status:** closed. PyPI publish + downstream pin bumps descoped.
**Gate met:** kernel dev→main merged (PR #8, HEAD `8d934c8`); antoncore legacy dirs removed.

### 1D — ~~PR + PyPI publish~~ (descoped)
- ~~Open 4 downstream dev→main PRs bumping kernel pin~~ — **n/a**: no downstream repo declares `agentibrain` in `pyproject.toml`. Kernel consumption is Helm chart + container image, not pip.
- ~~Drift-check CI on downstream repos~~ — **n/a**: same reason.
- ~~Cut `v0.1.0` tag → publish.yml → PyPI~~ — **descoped 2026-05-03**. Friend-install / PyPI moves to Block 3 if/when external adoption becomes a priority. `publish.yml` stays in place (dormant — fires only on future `v*.*.*` tag push).

### 1E — Post-merge cleanup (done)
- [x] Legacy chart directories absent from antoncore (`k8s/charts/anton-{kb-router,obsidian-reader,tick-engine,embeddings}` — verified 2026-05-03, none present).
- [x] No legacy ArgoCD apps in `antoncore/k8s/argocd/{dev,prod}/` matching `anton-{kb-router,obsidian-reader,tick-engine,embeddings}`.

---

## Block 2 — Prod cutover (Tier 2, queued)

**Status:** not started. Depends on Block 1 complete + dev soak ≥48h. Decoupling cutover (2026-04-30) shipped antoncore-owned overlays + ArgoCD apps in dev only — prod still sources from `agentibrain-kernel.git@main`. Both repos must merge dev→main to flip prod.
**Gate to next block:** prod smoke green, legacy prod StatefulSets scaled to 0, 24h prod observation clean.

### 2A — Prod storage + secrets
- [ ] `values-dev.yaml` / `values-prod.yaml` split confirmed on all kernel charts (already done in antoncore overlays — verify the split is preserved post-merge to main).
      **Accept:** both files exist in `antoncore/k8s/values-overlays/agentibrain-*/` with distinct image tags (`:dev` vs `:latest`).
- [ ] OpenBao `secret/k8s/agentibrain-*-prod` paths populated (embeddings, kb-router, obsidian-reader, agent-env).
      **Accept:** `vault kv list secret/k8s/ | grep agentibrain.*-prod` shows the expected paths.
- [ ] ESO ExternalSecrets synced in `anton-prod`.
      **Accept:** `kubectl get externalsecret -n anton-prod | grep agentibrain` all show `SecretSynced=True`.

### 2B — Prod deploy
- [ ] Prod ArgoCD apps under `antoncore/k8s/argocd/prod/agentibrain/` reconcile after dev→main merge — `agentibrain-embeddings-prod`, `agentibrain-kb-router-prod`, `agentibrain-obsidian-reader-prod`, `agentibrain-brain-keeper-prod`, plus `agentibrain-brain-cron` + `mcp-agentibrain` singletons.
      **Accept:** `argocd app list | grep agentibrain.*prod` all `Synced+Healthy`. `agentibrain-root-prod` source = `antoncore.git/k8s/argocd/prod/agentibrain`.
- [ ] Pods running in `anton-prod`.
      **Accept:** `kubectl get pods -n anton-prod | grep agentibrain | grep -c Running` ≥ 4.

### 2C — Client cutover
- [ ] Flip `EMBEDDINGS_URL` in prod `mcp-artifact-store.yaml` + Docker `stacks/artifact-store/compose.yml` to kernel service.
      **Accept:** artifact-store embedding writes land in kernel pgvector, confirmed via `SELECT count(*) FROM content_embeddings WHERE producer='…' AND created_at > now() - interval '5 minutes'`.
- [ ] Prod agents `BRAIN_URL` + `BRAIN_CLASSIFY_MODEL` + `BRAIN_BRIEF_MODEL` + `INFERENCE_API_KEY` flip — same wiring as dev but prod namespace.
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

**Status:** paused 2026-05-03. PyPI publish was descoped from Block 1, so the friend-install story (which assumes `pip install agentibrain`) effectively pauses too. Pull from Tier 3 backlog when external adoption becomes a priority.
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

---

## Block 5 — Decoupling residuals (new 2026-04-30)

**Status:** small cleanups discovered during the kernel decoupling cutover. None of these block any other block — opportunistic.

- [ ] Rewrite kernel `docs/{SECRETS,TROUBLESHOOTING,OPERATIONS}.md` — remove anton-namespace kubectl examples, replace with `<your-namespace>` placeholders. The deployment-artifact bleed is gone but doc bleed remains.
      **Accept:** `grep -rn 'anton-{dev,prod,ops}' docs/` returns empty.
- [ ] Add `examples/` tree to kernel — sample value overlays + ArgoCD `Application` CR templates with placeholder repo URLs / namespaces. Helps forkers see the deployment shape without inheriting Anton's.
      **Accept:** `examples/values-overlays/` and `examples/argocd/` exist with placeholder content; README links to them.
- [ ] Diagnose pre-existing `agentibrain-brain-cron` Degraded health (since `2026-04-30T16:13Z`, last 3 brain-cron jobs Failed). Not caused by decoupling, but live since before this work.
      **Accept:** root cause noted in `operator/incidents/` and either fixed or marked as expected behavior.
