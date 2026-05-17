# Changelog

All notable changes to **agentibrain-kernel**. Follows [Keep a Changelog](https://keepachangelog.com) + [Semantic Versioning](https://semver.org). Pre-1.0: minor bumps may carry breaking changes; patch bumps are non-breaking.

Tags are issued by the release workflow, not locally. See `docs/RELEASING.md` for the dispatch flow.

---

## [0.6.0] — 2026-05-17

**Brain self-healing**: closes a class of self-reinforcing degradations that pinned the dev brain at health score 2/10 with a stacking nuclear-signal loop. After this release, the brain produces real graph mutations every tick (was 0 for 30+ hours), the amygdala stops emitting from its own output, and the AI tick prompt sees the full 144-arc vault instead of 1.

### Features

- **`brain_apply.collapse_multi_type_same_pair`** — edges now collapse by `(source, target)` using a priority order (`parent > child > sibling > unblocks > supersedes > related`). Prior dedup keyed on `(source, type, target)`, so different type strings escaped and accumulated. 295 such collisions cleared from the live vault on first run.
- **Cross-tick `any_to_target` edge guard** in `apply_edges` — once a source has any edge to a target, subsequent emissions of a different type to the same target are silently rejected. Prevents multi-type accumulation across ticks forever.
- **`services/brain-ops/scripts/dedupe_edges.py`** — idempotent one-shot vault cleanup. Removes duplicate edges, self-loops, multi-type collisions; report distinguishes `dedup_dropped` from `type_collapsed`.
- **`services/brain-ops/scripts/tombstone_signals.py`** — companion to dedupe; marks active `@signal` blocks `severity=resolved` + `(CLEARED: ...)` prefix so the next tick drops them.
- **Lesson dedup in the AI tick prompt** keyed on first-line (what the AI actually sees), not full content. Empty-body lessons skipped.
- **Region-walk** ported into `brain_tick_prompt.build_prompt` AND `brain_apply.find_arc_file` / `apply_signal_changes`. The prompt previously scanned `clusters/<date>/*.md` only (1 arc survived filter, of 144 in vault); the apply side silently dropped every edge/signal whose source was a region-resident promoted arc.
- **Amygdala self-emission loop closed** (`brain_tick.py` + `amygdala.py`):
  - `brain_tick.py` publishes `brain.tick.complete` (was `tick.complete`) with an explicit `severity` field — nuclear only when the tick mechanism itself failed (parse-fail sentinel or `ai.error=true`); warning for score ≤ 3; info for score ≥ 4 (filtered out by amygdala).
  - `amygdala.py` defense-in-depth: skips `write_incident_arc` when `source=="brain-cron"` regardless of severity. The brain's own tick output is a status broadcast, not an incident.

### Bug Fixes

- `brain_apply.parse_edges` strips backticks from captured node IDs and drops self-loops at parse time.
- Dir-bucketed `merged_stems` in `brain_keeper._scan_and_collect`, `brain_tick_prompt._scan`, `brain_apply.find_arc_file` — under recursive scan, a `foo.merged.md` in one subdir no longer suppresses an unrelated `foo.md` in a sibling subdir. Latent shared bug, fleet-wide fix.
- `brain_tick_prompt._scan` now logs `WARN: failed to parse {file}: {e}` on parse error (parity with `brain_keeper`).
- `brain_tick_prompt.build_prompt` signal sample deduped by `(source, content_hash)` so a signal promoted to `frontal-lobe/conscious/` doesn't appear twice.
- `brain-api/vault_reader.search_vault` caps the line-hit score component at 30 — a 500-line journal file no longer crushes semantic search results.
- `Dockerfile` for `brain-ops` now copies `scripts/` so the maintenance tools ship in the image.

### Local-compose readiness

- `local/bootstrap.sh` scaffolds the full vault region tree via `cp -rn agentibrain/templates/vault-layout/`.
- `EMBEDDINGS_API_KEY` (singular, consumer-side) aligned to the same generated value as `EMBEDDINGS_API_KEYS` (plural, embeddings inbound whitelist).
- `.mcp.json.example` + README unified on `x-api-key` header (was contradictory).
- README tool count corrected to 5 (was 4 — `brain_ingest` was missing from the table), container count 7 (was 8 in `local/README.md`), port override variable renamed `PORT_KB_ROUTER` → `PORT_BRAIN_API`.

### Operations measured against dev cluster

| Metric | Pre-release | Post-release |
|---|---|---|
| `edges_applied` per tick | 0 (30+ hours) | 10–20 |
| Stacked nuclear `brain-cron` signals | 14 (growing +1/tick) | 0 (loop broken) |
| Health score floor | 2/10 | 6/10 |
| Vault arc count visible to AI | 1 | 146 |
| Multi-type edge collisions in vault | 295 | 0 |

### Known follow-ups (not in this release)

- `apply_merges` doesn't re-dedupe edges over the concatenated merged-arc body — AI flagged "triplicated edges" on `2026-04-28-7242ddf3-writer` after a merge.
- Empty Grafana panels for Broadcast Cortex / Hippocampus / Hook Observability — telemetry gap lives in `agentihooks-bundle`, not this kernel.

---

## [0.5.0] — 2026-05-12

**Architecture simplification**: `obsidian-reader` absorbed into `brain-api` as in-process `vault_reader.py`. Services renamed for consistency (`kb-router` → `brain-api`, `brain-cron` → `brain-ops`, `tick-engine` → `brain-ops`, `mcp-agentibrain` → `agentibrain-mcp`). MCP surface completed with native `brain_ingest`. Eliminates one pod, one image, one network hop.

### Breaking Changes

- **`obsidian-reader` service retired.** Image `ghcr.io/the-cloud-clockwork/agentibrain-obsidian-reader:*` is no longer published. Downstream consumers must remove references to it.
- **`kb-router` renamed to `brain-api`** — image `ghcr.io/the-cloud-clockwork/agentibrain-brain-api:latest`. The `agentibrain-kb-router` image is no longer published.
- **`brain-cron` and `tick-engine` consolidated into `brain-ops`** — image `ghcr.io/the-cloud-clockwork/agentibrain-brain-ops:latest`.
- **HTTP `/ingest` no longer requires the `artifact-store` external dependency** — body writes go straight to the vault filesystem via `vault_reader.write`.

### Features

- **5 MCP tools live**: `kb_search`, `kb_brief`, `brain_search_arcs`, `brain_get_arc`, `brain_ingest`. New `brain_ingest` writes text directly to the vault through `brain-api /ingest` (no artifact-store wrapper).
- **`brain_keeper` runs Phase 0 inbox drain on every tick** — `raw/inbox/*.md` is moved into region directories before scanning. Inbox drain also sets `region` + `status` frontmatter so arcs land where they belong.
- **`embed_arcs` + `brain_keeper` scan all region dirs** (`bridge/`, `left/`, `right/`, `frontal-lobe/`, `pineal/`, `amygdala/`) plus `clusters/`. Previously scanned only `clusters/` and missed 97% of arcs (4 → 144 arcs surfaced).
- **`brain_get_arc` uses direct `/by-key/{key}` DB lookup** on the embeddings service with vault fallback.
- **`brain-keeper` rebuilt as a self-contained agent package** — own `CLAUDE.md`, `command.yml`, `.mcp.json`, `.claude/settings.json`. The stale `agentihooks` profile is deleted (`AGENTIHOOKS_PROFILE=""`).
- **Brain profile rewritten with full tool inventory + auto-lookup rules + markers-vs-ingest decision table.**

### Bug Fixes

- `brain_keeper` `inject_blocks` deduped by SHA-256 content hash in `write_inject_feed`.
- `brain_keeper` skips promote/demote/graduate when source == destination.
- `brain_keeper.REGION_DIRS` lifted to module level for reuse.
- `brain-tick` AI prompt instructs the model to not emit duplicate signals for issues already in the active list.
- `mcp` SDK pinned `<1.26` — stdio framing incompatible with `mcp-proxy 6.4`.
- 5 bugs from a fresh code review pass: compose template, async boundary, path traversal, fallback URL, TOCTOU on file writes.
- Dead `OBSIDIAN_READER_TOKEN` removed from `k8s-bootstrap.sh`.

### Local-compose Quick Start (laptop path)

- `compose.yml` 7-container stack matching K8s topology, embeddings healthcheck uses Python urllib (no `curl` in image).
- `tick --no-ai` auto-detect when `INFERENCE_URL` is empty.
- `local/compose.ollama.yml` overlay for the free local LLM path.
- README Quick Start: 5 steps from `git clone` to a working brain with Claude Code MCP wiring.
- `activeDeadlineSeconds` bumped 600 → 1200 on brain-ops CronJob.

### Migration

Downstream Helm charts (antoncore) must:
1. Remove `obsidian-reader` Application + chart references.
2. Switch image pins from `agentibrain-kb-router` → `agentibrain-brain-api`.
3. Switch image pins from `agentibrain-brain-cron` / `agentibrain-tick-engine` → `agentibrain-brain-ops`.
4. Remove `OBSIDIAN_READER_URL` / `OBSIDIAN_READER_TOKEN` env from any callers (in-process now).

---

## [0.4.0] — 2026-05-04

**Documentation, observability, and naming consistency**: complete `anton/antoncore/friends/iamroot/homeofanton` scrub across user-facing docs + source, GitHub org rename (`the-cloud-clock-work` → `the-cloud-clockwork`), ecosystem prefix change (`tccw-` → `tcc-`), and self-contained Grafana enablement.

### Breaking Changes

- **GitHub org rename**: `the-cloud-clock-work` → `the-cloud-clockwork`. All image refs, Helm OCI references, and clone URLs must update.
- **Ecosystem prefix change**: `tccw-` → `tcc-` across chart names, image names, package names.

### Features

- **MCP chart** (`helm/mcp`) — 6th brain chart shipping the agentibrain-mcp service.
- **Self-contained Grafana enablement** — `setup-grafana` bootstrap script provisions datasources + dashboards from files, no manual UI setup.
- **brain-health Grafana dashboard JSON** shipped under `observability/`.
- **GitHub Pages site** via Jekyll + just-the-docs theme (4-section sidebar + Home).
- **`docker-build` workflow** now has `workflow_dispatch` for manual rebuilds.
- **Amygdala** points at Redis DB 11 and ticks announce themselves to the event bus.

### Bug Fixes

- `brain.tick_health` ClickHouse schema bootstraps idempotently on first tick.
- `brain-health` panel handles missing `signals` frontmatter (treats `'0'` and `false` as equivalent).
- `setup-grafana` respects existing file-provisioned datasources instead of attempting API-only injection.
- Jekyll Pages build excludes `agentibrain/` and `observability/` to avoid Liquid template collisions with template-syntax files.

### Docs scrub

- All references to `anton`, `antoncore`, `friends`, `iamroot`, `homeofanton` removed from user-facing docs + Python sources + tests + CLAUDE.md.
- Vault layout templates reframed from "operator" voice to "user/your" voice.
- README rewrite: six charts story, image publish flow, Claude Code wiring three-step.

---

## [0.3.0] — 2026-04-29

**Helm portability, ArgoCD parity, generic LLM gateway**: kernel decouples from operator-specific deployment shape. Charts ship portable defaults. Inference routes move from operator overlays to kernel with a generic gateway contract.

### Features

- **Generic LLM gateway contract** (`docs/brain/GATEWAY-CONTRACT.md`, `brain-models.yaml`). Inference routes live in the kernel and consume any OpenAI-compatible endpoint via `INFERENCE_URL` / `INFERENCE_API_KEY`.
- **Portable Helm chart defaults** + `k8s-bootstrap.sh` — clean install on a fresh cluster without operator-specific overlay paths.
- **ArgoCD brain Applications** imported into kernel `k8s/` with operator-overlay reference for downstream consumers.
- **NFS vault mount** wired through operator overlays + obsidian-reader dev profile.
- Inference routes migrated from operator overlays to kernel.
- `tccw-k8s-service-template` bumped to `0.3.6` (v1beta1 ESO).

### Bug Fixes

- ESO re-enabled in operator overlays (Pass A regression).
- `brain-keeper` pins agentihooks/bundle/hub to `dev` branch by default.
- `brain-keeper` wires `POD_NAME` + `AGENTIHOOKS_HOME` for per-pod state isolation.
- `brain-keeper` storageClass pinned to `local-path` in operator overlays.
- ArgoCD same-repo-revision parity + mcp-proxy chart source.

### Docs

- `HELM-QUICKSTART.md` + brain inference routes reference doc.
- README documents brain inference env vars + links to gateway contract.

---

## [0.2.0] — 2026-04-28

**Heat engine + `.merged.md` parity + local docker-compose deploy**: closes promote pipeline gaps and ships the non-K8s deployment path.

### Features

- **Local docker-compose deployment** (PR #9) for non-K8s users. Stack mirrors the K8s topology on a laptop.
- **Self-cleaning embeddings** (PR #14) — `POST /prune` endpoint + cron-wired reaper that drops embeddings for arcs deleted from the vault.

### Bug Fixes

- **Heat engine unstuck** (PR #10) — env-driven thresholds + softer decay curve. Arcs were getting stuck at low heat indefinitely.
- **`.merged.md` arcs included in tick processing** (PR #11) — was dropping 4 hot arcs from the promote pipeline.
- **Heat formula tolerates missing `signals` frontmatter** (PR #12) — uses 0 default instead of raising.
- **`embed_arcs` processes `.merged.md`** (PR #13) — parity with `brain_keeper`, was dropping ~40 arcs from semantic search.

---

## [0.1.1] — 2026-04-27

**Phase 8 Block 1+2 — kernel owns all 5 brain charts.**

Migration of `kb-router`, `obsidian-reader`, `embeddings` charts from `antoncore` into this kernel. `brain-keeper` and `brain-cron` bumped to `0.1.1` with amygdala template using `{{ .Chart.Name }}-amygdala`. All 5 render cleanly via `helm template`. 4 of 5 wrap `tccw-k8s-service-template v0.3.4` from `oci://ghcr.io/the-cloud-clock-work`. `brain-cron` keeps custom templates.

See [release notes for v0.1.1](https://github.com/the-cloud-clockwork/agentibrain-kernel/releases/tag/v0.1.1) for full chart inventory + ArgoCD multi-source consumption pattern.

---

[0.6.0]: https://github.com/the-cloud-clockwork/agentibrain-kernel/compare/v0.5.0...HEAD
[0.5.0]: https://github.com/the-cloud-clockwork/agentibrain-kernel/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/the-cloud-clockwork/agentibrain-kernel/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/the-cloud-clockwork/agentibrain-kernel/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/the-cloud-clockwork/agentibrain-kernel/compare/v0.1.1...v0.2.0
[0.1.1]: https://github.com/the-cloud-clockwork/agentibrain-kernel/releases/tag/v0.1.1
