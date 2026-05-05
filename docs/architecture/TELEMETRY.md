# Brain Nervous System — OpenTelemetry Observability

> **Shipped:** 2026-04-13
> **Scope:** Full 5-layer OTel pipeline + deterministic smoke test
> **Companion docs:** `docs/architecture/ARCHITECTURE.md`

## Why

The brain produces thoughts (marker emissions, broadcasts, injections). Before this work, to verify that a broadcast actually reached a session we had to ask the agent "did you see it?" — interrogating the brain instead of reading the nerve signal. This made validation slow, unreliable, and unscalable.

This doc describes the nervous system: the deterministic telemetry pipeline that observes every brain event as an OTel span or log, landing in ClickHouse + Langfuse, queryable from Grafana, and smoke-testable via a single CLI.

## Architecture

```
LAYER 1 — Claude Code native OTel (zero code)
  ~/.claude/settings.json env vars
    CLAUDE_CODE_ENABLE_TELEMETRY=1
    CLAUDE_CODE_ENHANCED_TELEMETRY_BETA=1
    OTEL_EXPORTER_OTLP_ENDPOINT=http://${OTEL_HOST}:4318
    OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
    OTEL_SERVICE_NAME=claude-code-home
  Emits: claude_code.user_prompt, claude_code.tool_result,
         claude_code.api_request, plus metrics (tokens, cost, active_time)

LAYER 2 — Agentihooks structured spans
  agentihooks/hooks/telemetry.py
    emit_span(name, attrs)
    span_ctx(name, attrs) → wraps block with duration
    emit_log(message, attrs) → OTLP /v1/logs fan-out
  Three instrumented functions emit three span types:
    brain.inject         brain_adapter._publish_entries
    brain.marker_write   brain_writer_hook.write_markers
    brain.delivery       broadcast.check_and_inject_broadcasts (per message)

LAYER 3 — Hook log OTLP fan-out
  agentihooks/hooks/common.py log() extended
  Events matching brain_* / broadcast_* / outbox_* / amygdala_*
  additionally POST to OTLP /v1/logs → ClickHouse otel_logs

LAYER 4 — Data plane
  otel-collector (Docker Compose at ${OTEL_HOST}:4318)
    ↓
  ClickHouse otel database  (otel_traces, otel_logs, otel_metrics)
  Langfuse via otlphttp/langfuse exporter (traces only)

LAYER 5 — Visualization
  Grafana dashboard /d/<your-dashboard-slug>/brain-nervous-system
    4 new panels: inject latency, marker write rate, delivery coverage, error rate
  Langfuse <your-langfuse-host>
    Per-session trace view: claude_code.user_prompt → brain.inject → brain.delivery(×N)
```

## Span taxonomy

| Span name | Fired by | Attributes | When |
|---|---|---|---|
| `brain.inject` | `brain_adapter._publish_entries` | `channel, entry_count, total_bytes, published_count` | Session start + every BRAIN_REFRESH_INTERVAL turns |
| `brain.marker_write` | `brain_writer_hook.write_markers` | `session_id, transcript_path, source, markers_found, outbox_count, redis_count, marker_types` | Stop hook (every turn) |
| `brain.delivery` | `broadcast.check_and_inject_broadcasts` | `session_id, message_id, channel, severity, source, bytes, persistent` | UserPromptSubmit, per broadcast message |
| `agentihooks.session.stop` | `hook_manager.on_stop` | `session_id, tool_calls, errors` | Stop hook (existing, pre-this work) |

## Log taxonomy

| Log prefix | Source | Purpose |
|---|---|---|
| `brain_*` | brain_adapter, brain_writer_hook | Deterministic audit of brain lifecycle |
| `broadcast_*` | broadcast.py | Publish/deliver/expire events |
| `outbox_*` | brain_writer_hook | Marker file writes |
| `amygdala_*` | amygdala_hook | Emergency signal reads |

All logs carry `service.name=agentihooks` resource attribute and land in `otel.otel_logs`.

## Query cookbook (ClickHouse)

```sql
-- All brain spans in the last hour
SELECT SpanName, count() FROM otel.otel_traces
WHERE SpanName LIKE 'brain.%' AND Timestamp > now() - INTERVAL 1 HOUR
GROUP BY SpanName;

-- Find all brain events for a specific session
SELECT Timestamp, SpanName, SpanAttributes
FROM otel.otel_traces
WHERE SpanAttributes['session_id'] = 'smoke-abc123'
ORDER BY Timestamp;

-- Slowest brain.inject calls in last 24h
SELECT Timestamp, Duration/1e6 AS ms, SpanAttributes['entry_count'] AS entries
FROM otel.otel_traces
WHERE SpanName='brain.inject' AND Timestamp > now() - INTERVAL 24 HOUR
ORDER BY Duration DESC LIMIT 20;

-- Marker write rate by type in the last 7 days
SELECT toStartOfDay(Timestamp) AS day,
       SpanAttributes['marker_types'] AS types,
       count() AS writes
FROM otel.otel_traces
WHERE SpanName='brain.marker_write' AND Timestamp > now() - INTERVAL 7 DAY
GROUP BY day, types ORDER BY day DESC;

-- Delivery coverage — which channels reached sessions today
SELECT SpanAttributes['channel'] AS channel,
       uniqExact(SpanAttributes['session_id']) AS sessions,
       count() AS deliveries
FROM otel.otel_traces
WHERE SpanName='brain.delivery' AND toDate(Timestamp) = today()
GROUP BY channel ORDER BY sessions DESC;

-- Recent brain errors from the hook log
SELECT Timestamp, Body, LogAttributes
FROM otel.otel_logs
WHERE SeverityText='ERROR' AND Body LIKE '%brain%'
AND Timestamp > now() - INTERVAL 1 HOUR
ORDER BY Timestamp DESC LIMIT 20;
```

## Langfuse usage

1. Navigate to `<your-langfuse-host>`
2. Filter service → your Claude Code service name (e.g. `claude-code-home`) or `agentihooks` (hook-emitted spans)
3. Pick a session trace → expand to see the causal graph:
   - `claude_code.user_prompt` (root)
     - `brain.inject` (from SessionStart/refresh)
     - `brain.delivery × N` (one per message delivered this turn)
     - `claude_code.tool_result × M` (tool calls the agent made)
     - `brain.marker_write` (on Stop)

Use Langfuse for "what did this specific session experience" — ClickHouse for "what's the aggregate system state".

## Smoke test — `brain-smoke`

Run after any change to brain code:

```bash
cd <path-to>/agentihooks
./scripts/brain-smoke                # 4 core tests, offline
./scripts/brain-smoke --otel-check   # + ClickHouse span verification (5 tests)

CLICKHOUSE_URL="http://default:$PASS@${CLICKHOUSE_HOST}:8123" \
  ./scripts/brain-smoke --otel-check
```

Tests:
1. `inject` — SessionStart payload → `broadcast.json` has brain entries
2. `delivery` — UserPromptSubmit → hook log grows
3. `marker_write` — Stop + lesson marker → outbox gains a file
4. `error_path` — Stop with empty `transcript_path` → graceful
5. `otel_spans` — ClickHouse has `brain.*` spans from this run

Exit 0 on all-pass, 1 on any fail. Runs in ~1.5 s. Wired into CI at `agentihooks/.github/workflows/brain-smoke.yml`.

## Troubleshooting

**Spans don't show up in ClickHouse**
- Check OTel collector is reachable: `curl http://${OTEL_HOST}:4318` → expect 404 (endpoint up, wrong path)
- Check env vars: `printenv | grep OTEL` → should show OTLP_ENDPOINT + PROTOCOL
- Check agentihooks telemetry flag: `printenv | grep OTEL_HOOKS_ENABLED` → should be `true`
- Restart your Claude Code session — settings.json env is read at startup

**Langfuse is empty**
- Langfuse only receives from the `otlphttp/langfuse` exporter in the collector
- Verify collector config: `ssh <otel-host> "grep -A3 langfuse <path-to-collector>/otelcol.yaml"`
- Verify credentials env: `ssh <otel-host> "docker exec <otel-collector-container> printenv | grep LANGFUSE"`
- For K8s pods: configure your `otel-collector` chart's langfuse exporter with credentials from your secret store (e.g. `<your-prefix>/otel-collector-prod`)

**ClickHouse lag > 30s**
- Check collector batch processor: default `timeout: 5s` — spans may batch for up to 5s before flush
- Query `otel.otel_traces` with a wider time window (`INTERVAL 5 MINUTE`)

**brain-smoke fails on delivery test**
- Means `hooks.log` didn't grow — either `LOG_HOOKS=false` or `~/.agentihooks/logs/` is read-only
- Fix: `mkdir -p ~/.agentihooks/logs && echo > ~/.agentihooks/logs/hooks.log`

**Telemetry is blocking hook execution**
- Should not happen (telemetry is fail-silent) but if it does: set `OTEL_HOOKS_ENABLED=false`
- The file log keeps working independently of the OTLP fan-out

## Files

| Layer | Path |
|---|---|
| 1 | `~/.claude/settings.json` (env block) |
| 2 | `agentihooks/hooks/telemetry.py` |
| 2 | `agentihooks/hooks/context/brain_adapter.py:151` |
| 2 | `agentihooks/hooks/context/brain_writer_hook.py:168` |
| 2 | `agentihooks/hooks/context/broadcast.py:500` |
| 3 | `agentihooks/hooks/common.py:102` (`log()` fan-out) |
| 4 | `<your-platform-repo>/stacks/observability/otelcol.yaml` (Docker collector) |
| 4 | `<your-platform-repo>/k8s/charts/otel-collector/values-{dev,prod}.yaml` (K8s collector) |
| 4 | `<your-platform-repo>/k8s/charts/otel-collector/templates/external-secret.yaml` |
| 5 | `<your-platform-repo>/stacks/observability/provisioning/dashboards/<your-dashboard>/brain-health.json` (4 new panels, ids 101-104) |
| 5 | `agentihooks/scripts/brain-smoke` |
| 5 | `agentihooks/.github/workflows/brain-smoke.yml` |
| 5 | `<your-platform-repo>/.claude/skills/brain-smoke/SKILL.md` |

## One-time setup

1. **Restart your Claude Code session** so the new `settings.json` env vars take effect.
2. **Populate your secret store** (optional, for K8s pod telemetry to reach Langfuse) with these keys at whatever path your collector overlay references:
   ```
   CLICKHOUSE_USER=default
   CLICKHOUSE_PASSWORD=<password>
   LANGFUSE_OTEL_ENDPOINT=<your-langfuse-host>/api/public/otel
   LANGFUSE_OTEL_AUTH=<base64 of pk:sk>
   ```
3. **Sync the collector deployment** (ArgoCD, Flux, or `helm upgrade`) to pick up the new exporter config.
