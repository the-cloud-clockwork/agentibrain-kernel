---
title: Readers Guide
parent: Concepts
nav_order: 2
---

# 8O — Brain System Reader's Guide

**Purpose:** how to *read* the brain. Not what it is — that's 8L/8M/8N. This is the field manual: every number, panel, broadcast, and arc decoded.

Use this when:
- You see a broadcast and want to know if it matters
- The dashboard shows a number and you need to know if it's bad
- You want to verify the brain is actually alive, not just claim it
- Something looks wrong and you need to trace it to a source of truth

---

## The Mental Model (30 seconds)

The brain is five cooperating systems:

1. **Clusters (vault)** — arcs = narrative units of work. One arc = one theme, accumulated over time. Stored as markdown at `<vault>/clusters/YYYY-MM-DD/<slug>.md` on shared storage.
2. **Ticks (cron)** — every 2h at HH:07 UTC, `brain-cron` scans arcs, computes heat, writes `brain-feed/*.md`, tombstones stale signals.
3. **Brain-feed (outbox)** — plaintext summary files (`hot-arcs.md`, `signals.md`, `inject.md`, `intent.md`, `last-tick-diff.md`, `health.jsonl`). Synced to every machine via rsync every 5min.
4. **Agentihooks (injection)** — on `UserPromptSubmit`, reads `brain-feed/*.md`, injects as `BROADCAST` blocks into Claude's context. Emits OTel spans per inject + delivery + marker-write.
5. **Brain-keeper (ops agent)** — first-class agent at `brain-keeper.<your-namespace>.svc:8200`. Runs triage, heal, replay, test via LiteLLM model `brain-keeper`.

Data flows: `sessions → clusters → tick → brain-feed → hooks → broadcast → new sessions (+ markers flow back)`.

---

## Reading the Broadcast

Every session starts with one or two `BROADCAST` blocks. They look like:

```
╔══════════════════════════════════════════════════╗
║  === BROADCAST [ALERT] ===                      ║
║  From: brain-adapter                             ║
║  [Active Signals]                                ║
║  - **[nuclear]** (auth-broker) - ...             ║
║  Expires: 2026-04-15T20:22:21Z                  ║
╚══════════════════════════════════════════════════╝
```

**Severity (in brackets):**
- `[ALERT]` → contains signals you should know about. Acknowledge.
- `[INFO]` → hot arcs, historical context. Background awareness.

**Signal severity (inline):**
- `[nuclear]` → FLEET HALT territory. Auth broken, data loss, security breach. Drop current task if unrelated.
- `[critical]` → service degraded. Operator attention needed. Don't ignore.
- `[warning]` → drift, approaching threshold. Acknowledge in context.
- `[info]` → FYI. Not actionable by itself.
- `[resolved]` → previously raised, now fixed. Will auto-tombstone next tick.

**Hot arcs table columns:**
- `Arc` → wikilink to vault file. Click path: `/vault/clusters/<date>/<slug>.md`
- `Heat` → 0-5. Higher = more recent activity. Decays 1 point per 2d after 2d age.
- `Region` → brain anatomy metaphor:
  - `left-hemisphere` = active/execution work (sessions, deploys)
  - `right-hemisphere` = strategy/reflection (plans, profiles)
  - `pineal` = scheduled/systemic (ticks, cron, observability)
  - `frontal-lobe` = decisions/hot priorities
  - `bridge` = cross-cutting (primitives, schemas)
  - `amygdala` = emergency signals only
- `Status` → `active` (in rotation), `graduated` (retired, below heat threshold).

**Expires** is the TTL. Broadcasts are re-evaluated every prompt.

---

## Reading the Dashboard

The kernel ships a starter Grafana dashboard at [`observability/brain-health.json`](https://github.com/The-Cloud-Clockwork/agentibrain-kernel/blob/main/observability/brain-health.json) — import it into your own Grafana instance, point it at the ClickHouse datasource the tick-engine writes to, and adapt as needed. Auth, hosting, and admin credentials are entirely your platform's concern; the kernel doesn't assume any particular secret store.

### Panel groups

**Frontal Lobe — Decisions & Hot Arcs**
- `Active Arcs` → count of non-graduated arcs. Healthy: 15-30. >50 = noise buildup, triage due.
- `Hot Arcs Written` → count of heat≥2 arcs at last tick. Healthy: 5-15.
- `Brain Activity (7d)` → three series: arcs scanned, signals collected, lessons extracted per tick. Flat zero line = tick broken.
- `Arc Mutations (24h)` → heat changes + promotions + demotions. Healthy: 10-50. Zero = decay engine stalled.

**Amygdala — Emergency Signals**
- `Signals (last tick)` → 0 is good. >5 = either a real incident or noise buildup.
- `Lessons (last tick)` → count of `@lesson` markers harvested. Healthy: 20-100 depending on session volume.
- `Heat Changes (last tick)` → arcs whose heat moved. 0 on every tick = suspicious (decay curve should trigger something in 2+ days).
- `Signals collected per tick (7d)` → time series. Spikes = real incidents. Sustained high = noise drowning the signal.

**Broadcast Cortex — Nervous System**
- `Injects / hour` → rate of broadcast deliveries. Matches session volume. Zero + active sessions = **OTel emission broken**.
- `Skip reasons (stacked)` → dedup / throttle / cap suppressions. Healthy ratio: dedup majority (we keep re-delivering the same broadcast).
- `Bytes injected — top sessions (1h)` → which sessions pay the broadcast cost. Outliers = long sessions.
- `Active broadcasts (live)` → messages currently in the bus.

**Pineal — Tick Circadian**
- `Health Score` → AI rating 1-10. Written by tick's AI reasoner. <5 = tick itself flagged issues (stale signals, arc pollution).
- `Last Tick` → timestamp. >3h old = cron stalled. Check `<your-ops-namespace>/brain-cron` CronJob.
- `Ticks Today` → should be 12 (every 2h = 12/day). <10 = failures or suspension.
- `Tick Duration (p50/p95)` → p50 20-30s, p95 <60s. Sustained >60s = AI reasoner slow or vault bloated.

**Hippocampus — Memory Markers**
- `Markers written (24h)` → count of `@lesson`/`@milestone`/`@decision`/`@signal` emissions harvested from transcripts. Reflects how much genuine learning the fleet captured.
- `Marker write latency p95` → should be <200ms. >1s = NFS contention or outbox saturation.
- `Markers / hour` → stacked by type. Lessons dominate during active work, milestones spike on block completions.

**Hook Observability**
- `brain.inject p50/p95` → latency of broadcast injection. p50 <50ms, p95 <300ms. Higher = NFS slow or brain-feed bloated.
- `Span emission by name (6h)` → three series: `brain.delivery`, `brain.inject`, `brain.marker_write`. All zero = agentihooks OTel broken (this exact failure mode fixed 2026-04-15).

### Empty panels rule

If ALL of `Injects/hour`, `brain.inject p50/p95`, and `Span emission by name` are flat zero → agentihooks isn't emitting OTel spans. Don't guess — query ClickHouse directly:

```sql
SELECT ServiceName, max(Timestamp), dateDiff('hour', max(Timestamp), now64(9)) as hours_ago
FROM otel.otel_traces
WHERE ServiceName='agentihooks'
GROUP BY ServiceName;
```

If `hours_ago > 2` while agents are working, OTel pipeline is broken. See §7 in TELEMETRY.md.

---

## Verifying the Brain is Actually Alive

Three independent signals must all agree:

**Signal 1 — Pod health**
```bash
kubectl get pods -A | grep -E "brain|amygdala"
```
Expect 4 brain-keeper + 1 amygdala, all Running. If any CrashLoopBackOff → describe, logs.

**Signal 2 — Last tick**
```bash
kubectl -n <your-ops-namespace> logs $(kubectl -n <your-ops-namespace> get pods -l job-name -o name | grep brain-cron | head -1) --tail=60 | grep -E 'arcs_scanned|signals_|total_ms'
```
Expect values. `total_ms` under 30000. `dry_run: false` on the live phase.

**Signal 3 — Span emission (last hour)**
```sql
SELECT SpanName, count()
FROM otel.otel_traces
WHERE ServiceName='agentihooks' AND Timestamp > now64(9) - INTERVAL 1 HOUR
GROUP BY SpanName;
```
Expect `brain.delivery` > 50, `brain.inject` > 5, `brain.marker_write` > 0.

**All three green → brain is alive.**
**Two of three green → degraded, investigate.**
**One or zero green → broken, read the corresponding subsystem doc.**

---

## Verifying a Specific Arc Landed

Operator asked: "I have a new ARC from today. How do I know it was registered?"

One-liner:

```bash
SLUG="<arc-slug>"   # e.g. 2026-04-15-983af4cc-writer
DATE="$(date -u +%Y-%m-%d)"

# 1. File exists on vault
ssh <your-vault-host> "ls <your-vault-path>/clusters/$DATE/ | grep $SLUG"

# 2. Broadcast references it (means tick picked it up)
grep -l "$SLUG" ~/.agentihooks/brain-feed/hot-arcs.md

# 3. Tick health OK after creation
tail -1 ~/.agentihooks/brain-feed/health.jsonl
```

All three → arc is registered and flowing into broadcasts.

---

## The Noise Problem (How to Spot It)

Tick health scores 5-6/10 mean the AI reasoner is finding structural issues. Most common:

| Complaint pattern | Root cause | Fix |
|---|---|---|
| "20+ single-session writer arcs" | Every session becomes its own arc | Run `brain-keeper triage` to merge by session UUID |
| "drill signals drowning real ones" | Stress-test signals not cleared | Stale-signal sweep running (filters >1d old, except nuclear/critical) |
| "nuclear X has no mitigation" | Active signal with no linked mitigation arc | Create mitigation arc OR mark signal `resolved` when fixed |
| "orphan arc" | Arc has no parent/sibling edges | Add `parent` or `sibling` frontmatter in the .md file |

---

## The Seven Marker Types (Output Protocol)

When you (or any agent) emit these in your output, `brain_writer_hook.py` harvests them on session Stop:

**Primary markers** (emitted by agents in normal output):

```markdown
<!-- @lesson -->
Specific technical insight that isn't in the docs.
<!-- @/lesson -->

<!-- @milestone status=done scope=X -->
A meaningful unit of work is complete.
<!-- @/milestone -->

<!-- @signal severity=warning source=Y -->
Something is broken/at-risk/resolved.
<!-- @/signal -->

<!-- @decision date=2026-04-15 -->
Architectural or design choice.
<!-- @/decision -->
```

**Infrastructure markers** (used by brain_keeper and brain_apply internally):

```markdown
<!-- @hot priority=8 -->
Force an arc to stay hot regardless of natural decay.
<!-- @/hot -->

<!-- @edge target=arc-id type=sibling|causal|temporal -->
Explicit relationship between arcs.
<!-- @/edge -->

<!-- @inject ttl=3600 -->
Content to inject into brain-feed directly.
<!-- @/inject -->
```

- All markers are HTML comments → invisible in rendered markdown.
- Max 5 per session (hook enforces for primary markers).
- Hook scans transcript on Stop → writes to `~/.agentihooks/brain-outbox/`.
- `@milestone` and `@signal` also XADD to Redis stream `events:brain` for real-time delivery.
- Next tick ingests outbox → becomes part of the arc narrative.
- Full spec: `MARKERS.md` (7 types with regex patterns and attributes).

---

## When Something Looks Wrong

| Symptom | First check |
|---|---|
| Broadcasts stop appearing | `~/.agentihooks/brain-feed/` file mtimes — rsync cron |
| Dashboard panels flat zero | OTel ClickHouse query (§ above) |
| Signals piling up | Last tick log — is sweep running? `BRAIN_STALE_SIGNAL_DAYS=1` env set? |
| Ticks stalled | `kubectl -n <your-ops-namespace> get cronjob brain-cron` — not suspended, last schedule recent |
| New arc not in hot list | Tick must run first. Wait until HH:07 UTC OR dispatch brain-keeper manually |
| Tick health score <5 | Read the `reason` field in `health.jsonl` — tells you exactly what's polluting |

---

## Invoking the Brain-Keeper

Three paths, use whichever:

**LiteLLM model** (fast, chat-style):
```
model: brain-keeper
```

**AgentiBridge dispatch** (parallel, durable):
```
mcp__tools-agent__agentibridge-run_agent
  agent_id: brain-keeper-0 | brain-keeper-1
  profile: brain-keeper
```

**Direct HTTP** (bypass LiteLLM):
```
POST http://<your-brain-keeper>.<your-namespace>.svc.cluster.local:8200/run
```

Commands (send as task prompt):
- `test` — 6-point self-diagnosis, uploads md+csv to `miscellaneous/brain-keeper/`
- `triage` — merge redundant arcs, graduate stale ones
- `heal` — 7-point drift audit
- `replay <arc-slug>` — re-execute an arc's workflow
- `tick` — manually fire a brain tick now (bypass cron schedule)
- `extract` — run the day's session extraction manually

---

## Source of Truth Table

| Question | Authoritative source |
|---|---|
| What arcs exist? | `<vault>/clusters/*/` on shared storage |
| What are current hot arcs? | `/vault/brain-feed/hot-arcs.md` (synced to `~/.agentihooks/brain-feed/`) |
| What signals are active? | `/vault/brain-feed/signals.md` |
| When did tick X run? | `~/.agentihooks/brain-feed/ticks/YYYY-MM-DDTHH-MM-SSZ-ai-output.md` |
| Tick health history | `~/.agentihooks/brain-feed/health.jsonl` (one line per tick) |
| Span events | ClickHouse `otel.otel_traces` |
| Log events | ClickHouse `otel.otel_logs` |
| Causal trace graph | Langfuse `<your-langfuse-host>` |

---

## Related Docs

- [`ARCHITECTURE.md`](ARCHITECTURE.md) — architecture, why the tick exists, hybrid deterministic+AI reasoning
- [`TELEMETRY.md`](TELEMETRY.md) — OTel span taxonomy, ClickHouse queries, troubleshooting
- [`KEEPER.md`](KEEPER.md) — brain-keeper agent internals, commands, report format
- [`MATURITY.md`](MATURITY.md) — maturity scorecard

---

## Stress Testing

Reproducible smoke tests for the brain pipeline. Run these to verify the full read/write/broadcast path.

### Test 1: Broadcast injection
```bash
dispatch_task("Report brain content in your context: Hot Arcs, Signals, BROADCAST, nuclear")
# Expected: agent reports 5+ BROADCAST blocks from brain-adapter
```

### Test 2: Marker write path
```bash
dispatch_task("Emit: <!-- @lesson -->Test lesson<!-- @/lesson -->")
# After completion: ls ~/.agentihooks/brain-outbox/  → new .json file
```

### Test 3: Overlay lifecycle
```bash
dispatch_task("python3 -c 'from scripts.overlay import overlay_add, overlay_remove; print(overlay_add(\"brain\")); print(overlay_remove(\"brain\"))'")
# Check broadcast.json for activate + deactivate messages
```

### Test 4: Redis XADD
```bash
dispatch_task("Emit: <!-- @signal severity=info source=test -->Test<!-- @/signal -->")
# Check: docker exec <redis-container> redis-cli -n 11 XLEN events:brain
```

### Test 5: Concurrent load
```bash
# Dispatch 3 agents in parallel, each emitting markers
# Verify: outbox has files from all 3, no conflicts (uuid filenames)
```

### Test 6: Forensic transcript check
```bash
# get_session MCP only returns user+assistant turns, NOT hook attachments
# For hook forensics, read raw JSONL: ~/.claude/projects/<project-dir>/<session_id>.jsonl
# Check attachment entries with hookName=SessionStart for brain content
```

### Common failure modes
| Symptom | Root cause | Fix |
|---------|-----------|-----|
| No broadcasts in dispatched agent | Missing `.agentihooks.json` at CWD | Create `$HOME/.agentihooks.json` with channels |
| `BRAIN_ENABLED=false` | Stale `.pyc` cache | `find agentihooks -name __pycache__ -exec rm -rf {} +` |
| brain_adapter publishes but broadcasts empty | SessionStart ordering wrong | brain_adapter BEFORE broadcast in hook_manager.py |
| Overlay blocked | Profile not in `allowedOverlays` | Add to profile.yml |
| Nested dispatch timeout | Chain of dispatches exceeds 300s | Simplify task or increase timeout |
