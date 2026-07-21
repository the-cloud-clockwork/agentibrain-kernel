# Changelog

All notable changes to **agentibrain-kernel**. Follows [Keep a Changelog](https://keepachangelog.com) + [Semantic Versioning](https://semver.org). Pre-1.0: minor bumps may carry breaking changes; patch bumps are non-breaking.

Tags are issued by the release workflow, not locally. Cut one by dispatching `.github/workflows/release.yml` with a `bump` of `patch`/`minor`/`major`.

---

## [0.7.0] — 2026-07-21

**Brain injection quality + on-demand ticks**: the brain now injects real
synthesized arc summaries backed by a working semantic index instead of
empty-stub scaffolding, and agents can force a tick on demand (`brain_tick` MCP
tool) so freshly-ingested content is retrievable immediately instead of waiting
for the 2h cron. Restores the injected context from near-useless (identical-stub
hot arcs, an index where every arc was equidistant) to a real, queryable memory.
The Compose deployment reaches parity with the Helm one on the same path, and
the update/redeploy procedure is documented for both.

### Features

- **Arc synthesis stage built** (`brain_tick_prompt.py` + `brain_apply.py`) — the
  summary pass was scaffolded but never implemented, so every arc carried an
  empty `synthesized: false` skeleton. The tick now generates a real one-sentence
  summary per arc, written to frontmatter and led into the injected hot-arcs
  table. Drain rate configurable via `BRAIN_MAX_SYNTH_ARCS`; backtick-wrapped
  `SUMMARY:` lines (the model copying the prompt's own format) are parsed, and the
  summary count is reported in the tick log.
- **`brain_tick` MCP tool** (`services/mcp/app/tools/tick.py`) — force a tick on
  demand: enqueues via `POST /tick` and blocks until it lands, so content written
  via `brain_ingest` or as `@lesson`/`@milestone`/`@signal`/`@decision` markers is
  retrievable through `kb_search` / `brain_search_arcs` immediately. `no_ai`
  exposes the sub-5s deterministic path.
- **On-demand tick made retrievable** — the tick-drain CronJob now runs
  `embed_arcs.py` after draining (it previously ran only `brain_tick.py`),
  refreshing the pgvector index so on-demand ticks surface new content. The drain
  also renders the full env map, so a drain-triggered tick uses the same tuning
  (decay/promote thresholds, `EMBEDDINGS_URL`) as the scheduled cron. Cadence
  dropped to every 1 minute.
- **Drain-time request coalescing** — redundant same-kind `POST /tick` requests
  are coalesced by the single serialized drain (one `brain_tick` per
  `(dry_run,no_ai)` kind) rather than at enqueue, which raced under concurrent
  callers and could wedge on an orphaned request file.
- **Env-configurable transcript extraction** (`EXTRACT_*`) and a `--source` label
  on the tick's event-bus announcement (`brain-drain` for on-demand) so on-demand
  ticks are distinguishable from the scheduled cron in the amygdala stream.

### Bug Fixes

- **Semantic index was noise** (`embed_arcs.py`) — `build_embed_text` never read
  `summary` and embedded the identical empty stub scaffolding present in every
  arc, so all arcs were roughly equidistant from every query (a uniform prior, not
  an index). `Summary:` now leads the embed blob, stub scaffolding is stripped, and
  `summary` is carried in embedding metadata. Requires a one-off
  `embed_arcs.py --force-all` to rebuild the index.
- **Summary truncation** mid-word, **unsummarized writer arcs** never offered for
  synthesis, and **duplicate inline markers** folded into arcs (36% of the vault) —
  all corrected.
- **`brain_tick` poll hang** — a `GET /tick/{job_id}` returning `unknown` (stale or
  consumed id) no longer polls the full timeout and falsely reports "queued"; it is
  terminal after a short grace. A `job_id`-less enqueue response is an explicit
  error rather than a silent skip of `wait`.
- **Atomic embed state write** — `embed_arcs` writes its mtime-state file via a temp
  file + `os.replace`, so the 1-min drain embed and the 2h cron embed colliding at
  HH:07 cannot corrupt it.
- **Invalid escape sequences** fixed, with a cache- and Python-version-independent
  CI guard (`check_escapes.py`) so they surface in CI instead of only warning on a
  fresh pod start.
- **Fresh arcs were hidden behind stale indexed heat** (`brain_search_arcs`) — the
  `min_heat` default of 2 filtered against the heat *snapshot taken when the arc was
  last embedded*, while heat is recomputed every tick. A just-ticked arc still reads
  0 there, as does a promoted arc sitting in `frontal-lobe/conscious`, so the default
  discarded exactly the newest content the caller was looking for. Default is now 0
  (rank by similarity); the parameter is documented as the stale-heat cut it is.
- **Un-summarized arcs were unfindable by their own content** (`embed_arcs.py`) —
  with no `summary` and no recognized sections, `build_embed_text` emitted only
  title and region, so an arc's body never reached the index. A body excerpt is now
  embedded as a `Content:` block in that case, making content-bearing arcs
  retrievable before the AI synthesis pass ever runs.
- **Compose `tick-drain` diverged from the Helm one** (`compose.yml`) — the Compose
  drain never ran `embed_arcs.py`, so on the Compose path a forced tick rewrote the
  vault while pgvector stayed stale and `brain_tick` reported success on content
  that stayed unsearchable. It also ran one tick per queued request instead of
  coalescing by kind, and omitted the `BRAIN_DECAY_*` / `BRAIN_STALE_SIGNAL_DAYS`
  tuning that `tick-cron` carries, so drain ticks and scheduled ticks could
  promote/demote the same arc differently. All three corrected.
- **Helm and Compose disagreed on signal ageing** — `helm/brain-ops/values.yaml` shipped
  `BRAIN_STALE_SIGNAL_DAYS: "1"` while Compose and the code default in
  `brain_keeper.py` both use `3`, so stock Helm aged signals out three times faster
  than stock Compose for no stated reason. Helm now matches the code default. **This
  changes behaviour for anyone running stock chart values**; pin it back in your
  overlay if you relied on 1-day ageing.
- **Four of five charts pinned a tag that does not exist** — `brain-api`, `brain-ops`,
  `embeddings`, and `mcp` all defaulted to `image.tag: latest`, which CI has never
  published, so a stock `helm install` produced `ImagePullBackOff` on every one of
  them. All now default to `:dev`, verified by rendering each chart. This is the
  functional counterpart to the `:latest` documentation error below — the docs
  described a tag that did not exist and the charts tried to pull it.

### Documentation

- **Compose update/redeploy procedure documented** (`local/README.md`, `README.md`).
  Compose builds from the source tree, so `docker compose up -d` after a `git pull`
  silently keeps running the old image — `--build` is the step that makes a change
  take effect, and that was written down nowhere. Adds branch guidance (`dev` =
  newest, `main` = stamped snapshot), a source-directory → service rebuild map, how
  to verify the new code is actually live, and what survives an update.
- **`CLAUDE.md` gained a "Redeploying after a code change" section** so an agent
  working in a fresh clone detects which path the checkout is running (Compose vs
  Kubernetes) and follows the correct one, rather than defaulting to editing a live
  container.
- **`:latest` corrected** — `local/README.md` told readers to pull
  `agentibrain-<service>:latest`, which CI has never published; only `:dev` exists,
  so that instruction produced a pull failure. `main` is also described accurately
  as the snapshot branch (it publishes no image and deploys nothing) in place of the
  stale "vestigial" wording in `README.md`, `CLAUDE.md`, `docs/DEPLOYMENT.md`, and
  `docs/ENVIRONMENTS.md`.
- **Compose topology corrected** — the local guide described 7 containers and omitted
  `tick-drain` from its diagram; the stack is 8, and the on-demand tick path is now
  documented via `POST /tick` (which also refreshes the index) rather than a
  `docker compose exec` into `tick-cron` (which does not).
- **`:latest` purged repo-wide** — the tag was referenced as a real, pullable image in
  `docs/GLOSSARY.md`, `docs/MCP.md`, `docs/TROUBLESHOOTING.md`, `docs/architecture/
  ARCHITECTURE.md`, `docs/architecture/CLUSTERS.md`, `helm/README.md`, `index.md`, and
  `README.md`. CI has only ever published `:dev`, so every one of those instructions
  fails. `docs/TROUBLESHOOTING.md` was the sharpest case: its remedy told the reader to
  "force docker-build to rebuild `:latest`", a build that structurally cannot happen.
- **MCP tool count corrected** — `README.md` advertised 5 tools and `docs/MCP.md` 4;
  `services/mcp/app/server.py` registers 6. `brain_tick` was missing from the tool table
  a reader checks `/mcp` against.
- **Image count corrected** — six Compose services declare a build but share only four
  images (`tick-cron`, `tick-drain`, and `amygdala` are one `brain-ops` image with
  different entrypoints), so "rebuilding all six images" overstated the work by 50%.
- **Healthcheck expectation corrected** — the docs told the reader to run
  `docker compose ps` and expect "all healthy", which three of eight services can never
  report: `services/brain-ops/Dockerfile` declares no `HEALTHCHECK`, so `tick-cron`,
  `tick-drain`, and `amygdala` show a bare `Up` by design. The docs now say so, rather
  than priming a first-run user to read correct behaviour as a fault.
- **Non-existent MinIO removed** — `docs/ENVIRONMENTS.md` claimed the root Compose stack
  ships an object store; it ships Postgres and Redis. (MinIO exists only in the `brain`
  CLI's rendered template, a different path.) The stale `services/tick-engine/` build
  path in `docs/architecture/ARCHITECTURE.md` was also corrected to `services/brain-ops/`.
- **`docs/operations/BRAIN-INJECTION-REPAIR.md` added** — the work order and outcome for
  this release's headline repair (arc synthesis + the embedding index), previously
  shipped without a changelog entry.
- **Broken self-reference fixed** — `CHANGELOG.md` pointed at `docs/RELEASING.md`, which
  does not exist in this repo. It now names the actual mechanism: dispatch
  `.github/workflows/release.yml` with a `bump` input.

### Security

- **Deployment-specific detail removed from the public repo** — a private LAN NFS
  server address and namespace were hardcoded in
  `helm/brain-ops/jobs/vault-cleanup.yaml`; these are now `<your-*>` placeholders
  with a PVC alternative shown. Operator-specific platform names were also removed
  from `docs/ENVIRONMENTS.md`, `helm/brain-ops/values.yaml`, an operations write-up
  that quoted real vault arc content, and sample cluster IDs in source comments and
  tests. No credential values were present at any point.
- **Real private session data removed from the architecture docs** — the "Cluster
  Primitive — Schema" example in `docs/architecture/CLUSTERS.md` was not synthetic: it
  carried two real session UUIDs with their turn counts, nine real PR numbers from a
  private repo, a real secret-store variable rename, and a private service scheme.
  `docs/architecture/KEEPER.md` carried a real internal gateway `model_id` UUID, a real
  cross-repo PR reference, and real dated run identifiers; `docs/architecture/
  MATURITY.md` carried the same run identifiers plus an internal planning codename.
  All replaced with synthetic values that preserve the schema shape and the reusable
  lessons.
- **The de-branding changelog entry was itself the leak** — the historical 0.6.x entries
  named the exact strings the scrub had removed, republishing the operator's account
  name, private domain, and deployment names in a public repo. Those entries now
  describe the change without naming what was scrubbed.
- **`docs/operations/BRAIN-INJECTION-REPAIR.md` generalized** — a real incident
  postmortem quoting live transcript content. The credential-echo lesson that motivated
  `redact.py` is kept; the estate-identifying specifics are gone. No credential value
  was ever committed — only a path to a file on private storage.
- **Dead `operator/` links removed from `README.md`** — the directory is gitignored, so
  five links 404'd for every reader while confirming the structure of a private planning
  directory. Points at the published `docs/architecture/MATURITY.md` instead.

- **Credential redaction at every persist and inject boundary** (`redact.py`) — the
  tick echoed its own prompt input, amplifying a leaked token across dozens of vault
  files. Every persist point (AI-output audit, intent, summaries) and inject
  boundary now scrubs credentials (superset secret regex plus `NAME=value`
  assignments).

### Known follow-ups (not in this release)

- Concurrent `brain_tick` (scheduled cron + a non-empty drain at HH:07) can still
  race on vault file writes — pre-existing; the safe fix is atomic writes in
  `brain_keeper`'s output layer, not an NFS lock (unreliable across pods, and a
  blocking lock risks a stale-lock hang that would fail the drain).
- The `brain_tick` MCP tool is callable only after the MCP gateway re-lists tools
  (operator-managed propagation).
- ClickHouse `tick_health.tick_type` still records `'full'` for on-demand ticks; the
  event-bus `source` field distinguishes them, but the ClickHouse column would need
  a schema migration.

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
- **`kb-router` renamed to `brain-api`** — image `ghcr.io/the-cloud-clockwork/agentibrain-brain-api:dev`. The `agentibrain-kb-router` image is no longer published.
- **`brain-cron` and `tick-engine` consolidated into `brain-ops`** — image `ghcr.io/the-cloud-clockwork/agentibrain-brain-ops:dev`.
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

Downstream Helm charts (your platform repo) must:
1. Remove `obsidian-reader` Application + chart references.
2. Switch image pins from `agentibrain-kb-router` → `agentibrain-brain-api`.
3. Switch image pins from `agentibrain-brain-cron` / `agentibrain-tick-engine` → `agentibrain-brain-ops`.
4. Remove `OBSIDIAN_READER_URL` / `OBSIDIAN_READER_TOKEN` env from any callers (in-process now).

---

## [0.4.0] — 2026-05-04

**Documentation, observability, and naming consistency**: complete scrub of operator-specific names across user-facing docs + source, GitHub org rename (`the-cloud-clock-work` → `the-cloud-clockwork`), ecosystem prefix change (`tccw-` → `tcc-`), and self-contained Grafana enablement.

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

- All operator-specific deployment, host, and account names removed from user-facing docs + Python sources + tests + CLAUDE.md.
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

Migration of the `kb-router`, `obsidian-reader`, and `embeddings` charts from the downstream platform repo into this kernel. `brain-keeper` and `brain-cron` bumped to `0.1.1` with amygdala template using `{{ .Chart.Name }}-amygdala`. All 5 render cleanly via `helm template`. 4 of 5 wrap `tccw-k8s-service-template v0.3.4` from `oci://ghcr.io/the-cloud-clock-work`. `brain-cron` keeps custom templates.

See [release notes for v0.1.1](https://github.com/the-cloud-clockwork/agentibrain-kernel/releases/tag/v0.1.1) for full chart inventory + ArgoCD multi-source consumption pattern.

---

[0.6.0]: https://github.com/the-cloud-clockwork/agentibrain-kernel/compare/v0.5.0...HEAD
[0.5.0]: https://github.com/the-cloud-clockwork/agentibrain-kernel/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/the-cloud-clockwork/agentibrain-kernel/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/the-cloud-clockwork/agentibrain-kernel/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/the-cloud-clockwork/agentibrain-kernel/compare/v0.1.1...v0.2.0
[0.1.1]: https://github.com/the-cloud-clockwork/agentibrain-kernel/releases/tag/v0.1.1
