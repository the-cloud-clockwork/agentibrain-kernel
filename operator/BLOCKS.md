---
id: agentibrain-kernel-blocks
title: agentibrain-kernel — Active Blocks
project: agentibrain-kernel
status: active
updated: 2026-05-03
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
- [x] Legacy chart directories absent from antoncore (`k8s/charts/anton-{brain-api,obsidian-reader,tick-engine,embeddings}` — verified 2026-05-03, none present).
- [x] No legacy ArgoCD apps in `antoncore/k8s/argocd/{dev,prod}/` matching `anton-{brain-api,obsidian-reader,tick-engine,embeddings}`.

---

## Block 2 — Prod cutover (mostly done — close-out 2026-05-03)

**Status:** prod cutover de-facto already shipped. Antoncore main carries `k8s/argocd/prod/agentibrain/` + `k8s/values-overlays/agentibrain-*/values-prod.yaml`. All prod brain pods Running 29h–45h. Remaining work is the brain-ops singleton fix + smoke + 24h observation.
**Gate to close:** brain-ops `SharedResourceWarning` cleared, prod smoke matrix green, 24h post-smoke clean.

### 2A — Prod storage + secrets (done)
- [x] Antoncore `k8s/values-overlays/agentibrain-{brain-api,embeddings,obsidian-reader,brain-keeper}/values-prod.yaml` present on main with distinct `:latest` image tags.
- [x] OpenBao `secret/k8s/agentibrain-*-prod` paths populated — implicit by virtue of prod pods running with valid env (45h uptime, no auth errors in brain-api-prod logs).
- [x] ESO ExternalSecrets synced in `anton-prod` — same evidence.

### 2B — Prod deploy (done)
- [x] Prod ArgoCD apps reconcile from `antoncore.git@main` `k8s/argocd/prod/agentibrain` — `agentibrain-{embeddings,brain-api,obsidian-reader,brain-keeper}-prod`, `mcp-agentibrain` all Synced.
- [x] ≥4 prod pods Running: `agentibrain-{brain-api,embeddings,obsidian-reader,brain-keeper}-0` + `mcp-agentibrain-0`, all `1/1 Running` for 29h+.

### 2C — Client cutover (done — original framing was wrong)
- [x] ~~Flip EMBEDDINGS_URL~~ — **n/a**: retired 2026-04-26 with brain-blind boundary (`stacks/artifact-store/src/resolver.py:31`). artifact-store no longer auto-embeds.
- [x] BRAIN_URL flipped on prod agents — every chart at `k8s/charts/{agenticore,anton-agent,publisher,finops-agent,diagram-agent,video-editor-agent}/values-prod.yaml` points at `http://agentibrain-brain-api.anton-prod.svc:8080`.
- [x] ~~BRAIN_CLASSIFY_MODEL/BRAIN_BRIEF_MODEL/INFERENCE_API_KEY on agents~~ — **misframed**: these are kernel-side env (only brain-api + brain-ops call the LLM). Set in `k8s/values-overlays/agentibrain-brain-api/values-prod.yaml` already.
- [x] ~~`anton-brain-api.anton-prod.svc` service alias~~ — **n/a**: zero callers reference the legacy URL (`grep -rn 'anton-brain-api' k8s/ stacks/` returns empty), no alias needed.

### 2D — brain-ops singleton + smoke + observation (open)
- [x] Resolve `agentibrain-brain-ops-prod` SharedResourceWarning — antoncore PR `chore/block2-close-prod-cutover` deletes the prod-tracking ArgoCD Application (singleton lives under `agentibrain-brain-ops`, dev-tracking).
- [x] `agentibrain-embeddings-prod` + `agentibrain-brain-keeper-prod` `Progressing` — pods 1/1 Running 29h+. ArgoCD will reconcile to Healthy after antoncore dev→main merge + next self-heal cycle. Same root pattern noted under Block 5.
- [x] Prod smoke executed 2026-05-03 — `/feed /signal /marker /ingest` all 2xx. Idempotency replay verified.
- [ ] 24h prod observation — error count from brain-api-prod + brain-keeper-prod logs (re-check 2026-05-04).
      **Accept:** zero new error spikes vs prior 24h baseline.
- [x] Legacy `anton-embeddings` / `anton-brain-api` / `anton-obsidian-reader` / `anton-tick-engine` already absent from `anton-prod` and `antoncore/k8s/charts/` (mirror of 1E).

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

- [x] Doc anton-scrub done 2026-05-03. Replaced operator-specific tokens (`anton-{dev,prod,ops}`, `claude-max-{haiku,sonnet}`, `10.10.30.*`, `litellm/auth-broker/agentibridge.anton-*.svc`, dashboard slug `anton-brain-health`) with `<your-*>` placeholders across `docs/{SECRETS,TROUBLESHOOTING,OPERATIONS,DEPLOYMENT,GLOSSARY,MIGRATION}.md` + `docs/architecture/{KEEPER,READERS-GUIDE,CLUSTERS,ARCHITECTURE,TELEMETRY}.md`. ENVIRONMENTS.md kept its anton refs as the operator-reference walk-through but with a generic disclaimer header. `openbao` references softened in SECRETS/DEPLOYMENT/TROUBLESHOOTING (it's the operator's ClusterSecretStore name, framed as substitutable).
      **Verified:** `grep -rEn 'anton-(dev|prod|ops)\b|claude-max-(haiku|sonnet)|10\.10\.30\.' docs/` returns zero hits outside ENVIRONMENTS.md.
- [x] `examples/` tree shipped 2026-05-03. `examples/values-overlays/{brain-api,embeddings,obsidian-reader,brain-keeper,brain-ops}/` (8 overlay files) + `examples/argocd/{dev,prod}/` (10 Application CRs) + `examples/argocd/agentibrain-root.yaml.example` + `examples/README.md` documenting placeholders + singleton-vs-per-env distinction.
- [x] Diagnose `brain-ops` job non-completion — root cause: stale `INFERENCE_API_KEY` in `secret/k8s/brain-inference` (OpenBao restored from 2026-04-30 restic post-NVMe recovery, LiteLLM dev DB rebuilt from current state, key hashes diverged → HTTP 401 in Phase 3 AI synthesis). **Fixed 2026-05-03** by adding `rotate_file` dispatch input to `litellm-state/.github/workflows/reconcile.yml` and dispatching against `units/brain-inference.json`. New key prefix `sk-gH0F`, ESO synced, manual `brain_tick.py` re-run completed exit 0 with 14231ms total (Phase 3 produced LLM output). Live broadcast `[Active Hot Arcs]` shows fresh today's session arc heat=6.
- [x] `agentibrain-brain-ops-prod` ↔ `agentibrain-brain-ops` SharedResourceWarning: antoncore PR drops the prod variant (singleton). Dev currently shows `OutOfSync` because antoncore main still carries the prod variant; resolves on antoncore dev→main merge.
- [x] `agentibrain-brain-keeper` (dev + prod) `Progressing` — pods 1/1 Running, sts ready, ArgoCD reports stale rollout state. Will reconcile on next ArgoCD self-heal cycle.
