# Brain System Maturity Assessment

> **Last assessed:** 2026-04-18
> **Assessor:** Self-reviewed after the brain-keeper first-class agent sprint
> **Overall maturity:** **~70%** (revised 2026-04-18, was ~80%)
> **Status:** Core functional, production-usable, self-observing. Not yet hardened against failure.

---

## Executive verdict

**The nervous system is ALIVE and auditable.** The operator can dispatch brain-keeper from any interface (Telegram via a router agent → AgentiBridge → brain-keeper, or direct LiteLLM `model: brain-keeper`, or `run_agent` from another session) and receive a structured markdown + CSV report that auto-mirrors to Google Drive within 60 seconds as a native Google Doc + Google Sheet. The reports are readable from a phone, sortable chronologically by filename (`brain-report-YYYY-MM-DD-HHMM.{md,csv}`), and brain-keeper itself can audit its own environment and flag misconfigurations the operator made.

That closed loop — dispatch → audit → artifact → operator read → apply recommendations — is the 70%. The remaining 30% is polish, resilience, and self-flagged follow-ups.

---

## Breakdown by area

| Area | Weight | Maturity | Notes |
|---|---|---|---|
| **Core pipeline** (read/write/amygdala/replay/extraction) | 25% | **88%** | Blocks 1-5 shipped. Nuclear halt deferred (B3). First real /replay E2E never executed (B4). 5/6 planned signal sources live (missing: ArgoCD webhook, k8s informer). |
| **Observability** (OTel, dashboards, spans) | 15% | **100%** | 5-layer OTel pipeline shipped. 27-panel Grafana dashboard across 6 biological regions. brain.inject/delivery/marker_write spans in ClickHouse. Langfuse trace correlation. |
| **Agent surface** (brain-keeper, A2A, LiteLLM model, reports→Drive) | 20% | **92%** | brain-keeper first-class ops oracle. LiteLLM model + AgentiBridge dispatch. Drive reports (Doc+Sheet). Daily test+triage crons live in ops.yaml. **Gap:** Opus activation pending (values.yaml override). |
| **Self-healing** (cadence tuning, noise cleanup, I/O profiling) | 15% | **42%** | Detection loop running: `reasoner_feedback.py` (every 6h), `heal.py` 7-point audit (daily 05:53), `brain-triage-daily` (daily 03:31), `brain-heal-daily`, `brain-feedback-hourly`. Cadence tuning, noise purge, marker_write I/O profiling still pending. |
| **Resilience** (vault backup, outage tests, concurrent writes) | 10% | **15%** | `brain-backup-daily` cron live (02:43 UTC, S3 target). No chaos testing. No DLQ for brain-cron failures. No concurrent write stress test. |
| **Advanced** (profile broadcast, bare claude wrapper, /replay E2E, custom MCP unit) | 15% | **40%** | Profile activation broadcast designed but unshipped. Bare `claude -p` wrapper half-shipped. First real `/replay` E2E never executed. `brain-keeper-tools-prod` unit exists but not consumed. |

**Weighted total: 0.25×88 + 0.15×100 + 0.2×92 + 0.15×42 + 0.1×15 + 0.15×40 = 69.2% → ~70%**

---

## What the 70% means in practice

### What works end-to-end today

- **Dispatch surface:** LiteLLM `POST /v1/chat/completions` with `model: brain-keeper` OR AgentiBridge `run_agent(brain-keeper-0, task)`.
- **Command surface:** `tick`, `test`, `triage`, `enrich <arc>`, `replay <arc> --to <target>`, `extract clusters`, `dashboard`. Free-form prompts interpreted loosely.
- **Report pipeline:** Markdown + CSV → artifact-store REST PUT → S3 (`<your-artifacts-bucket>/miscellaneous/brain-keeper/`) → drive-sync Lambda → Google Drive (`misc/brain-keeper/`) → auto-convert `.md → Google Doc` and `.csv → Google Sheet`.
- **Timestamped naming:** `brain-report-YYYY-MM-DD-HHMM.{md,csv}` (UTC minute granularity). Chronologically sortable in Drive.
- **Self-audit:** brain-keeper queries its own environment via ClickHouse, writes a 6-region report (Frontal/Amygdala/Broadcast/Hippocampus/Pineal/Hook Observability), emits PASS/WARN/FAIL per check, recommends fixes.
- **Cost per run:** ~$0.15 (Sonnet 4.6, ~40 seconds wall time). Opus target would increase this 3-5×.
- **Observability:** every brain action emits OTel spans → ClickHouse → Grafana. No more agent interrogation to verify anything.

### Validated runs on 2026-04-13

- `brain-report-2026-04-13-2207` — first run, 4268 bytes markdown, 12 PASS / 1 WARN / 2 FAIL, uploaded S3
- `brain-report-2026-04-13-2320` — second run, 5069 bytes md + 2835 bytes csv, new naming convention, both formats uploaded to `miscellaneous/brain-keeper/`, drive-sync confirmed within ~60s

### Known self-diagnosed issues (fragility)

Brain-keeper audited its own environment and reported these. **Single source of truth:** `docs/brain/KEEPER.md` § Known Issues (items F.8–F.14). Do not duplicate the list here — see 8N for current status and fixes.

---

## Path to 100%

### Quick wins (~70% → ~80%, low effort)

| Action | Effort | Impact |
|---|---|---|
| Fix `AGENT_MODE_MODEL: opus` in values.yaml | 1 line, 5 min | Brain-keeper reasoning tier unlocked |
| Loosen cadence (`BROADCAST_MAX_PER_PROMPT=2`, `MIN_INTERVAL_SEC=300`) | 2 lines + pod env, 10 min | Throttle drops below 50% (F.11 fixed) |
| Daily brain-test cron (`workflows/crons/brain-test-daily.yaml`) | 1 file, 15 min | Drive report every morning, self-audit loop closes |
| Validate agenticore + agentibridge image rebuilds | 2× CI watch, 10 min | Live patches become durable (F.9 + AgentiBridge wait fix) |

### Medium effort (~80% → ~90%)

- Profile `marker_write` NFS I/O, identify contention source, apply fix (F.12)
- Purge noise arcs + auto-demote zero-edge hot arcs in `brain_keeper.py` (F.13, F.14)
- First real `/replay` E2E against a reproducible arc + verify `replayed_from` edge creation
- Bare `claude -p` wrapper for non-Agent-tool dispatched sessions
- Consume `brain-keeper-tools-prod` custom unit (add entry to `agentihooks-bundle/.claude/.mcp.json` + key env)

### Hard / operator input needed (~90% → ~100%)

- **Vault backup** — operator decides destination (restic to second disk? rclone to S3? Drive as tertiary?)
- Chaos testing: NFS outage, Redis outage, concurrent write race conditions
- Arc count scaling beyond 200 arcs (measure quick-refresh + full tick latency)
- Ship `atomic-drifting-catmull.md` profile activation broadcast plan

---

## Historical context

| Phase | Maturity after | Milestone |
|---|---|---|
| Phase 0 (2026-04-09) | ~30% | Extraction + cluster scaffolding, 12 arcs baseline |
| Block 1 shipped (2026-04-10) | ~45% | K8s CronJob + deterministic pipeline |
| Block 2 shipped (2026-04-10) | ~55% | Hot-arc injection via brain-keeper CronJob |
| Block 3 shipped (2026-04-11) | ~65% | Amygdala event bus consumer + signal file path |
| Block 4 shipped (2026-04-12) | ~70% | Replay skill + semantic search + workflow extractor |
| Block 5 shipped (2026-04-13) | ~75% | Real-time marker write path + fleet broadcast |
| OTel pipeline + dashboard redesign (2026-04-13) | ~77% | Observable from both sides, no more agent interrogation |
| **brain-keeper first-class agent (2026-04-13)** | **~80%** | **Dispatch surface + Drive reports = operator-ready** |
| Self-healing crons + heal.py + feedback loop (2026-04-14) | ~75% | Detection infrastructure running, cadence tuning pending |
| Documentation audit + maturity re-assessment (2026-04-18) | ~70% | Honest re-scoring: core pipeline 88% (deferred criteria), self-healing 42% (crons counted) |

---

## Cross-references

- `docs/brain/ARCHITECTURE.md` — brain core (arcs, vault, brain-tick, brain-feed)
- `docs/brain/TELEMETRY.md` — OpenTelemetry 5-layer pipeline
- `docs/brain/KEEPER.md` — brain-keeper first-class agent spec, command surface, report pipeline, readiness 7.5/10
- `operator/BRAIN-MVP.md` — sprint plan, block status, fragility list, priority queue
- `operator/BLOCKS.md` — high-level brain block in active projects
- `operator/architecture/AGENT-ROSTER.md` — brain-keeper row with evolved scope
