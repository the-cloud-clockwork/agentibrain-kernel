# Brain Keeper — First-Class Brain Ops Agent

**brain-keeper is the single point of contact for every brain/vault operation.** Operators and other agents dispatch natural-language tasks to it and receive structured markdown + CSV reports that auto-mirror to Google Drive as Google Docs + Google Sheets. No skill rot, no opaque pod internals — the agent IS the skill surface.

---

## Identity

- **Kind:** K8s StatefulSet running the agenticore image with `AGENT_MODE=true`
- **Namespace/pod:** `anton-prod/brain-keeper-0` (dev mirror in `anton-dev`)
- **Port:** `8200` (HTTP OpenAI-compat + `/health`)
- **Profile:** `brain-keeper` (agentihooks-bundle)
- **Package:** `agentihub/agents/brain-keeper/` — `agent.yml`, `package/system.md`, `package/.agentihooks.json`
- **Model:** Sonnet 4.6 (brain-keeper orchestrator — Opus is the target, see Known Issues)
- **Concurrency:** `AGENTICORE_MAX_PARALLEL_JOBS=3`
- **Timeout:** 600s per job
- **A2A registration:** auto-registers with AgentiBridge on startup (`registered_at` in `list_agents`)

## Command surface

brain-keeper's `system.md` defines 7 commands. Operators send free-form prompts; the agent interprets loosely.

| Command | What it does |
|---|---|
| `tick` | Vault hygiene — read arcs, recompute heat, promote/demote, write `_hot-arcs.md`, update dashboards. Original 30-min cron job. |
| `test` | End-to-end brain health audit. Produces markdown + CSV report, uploads to artifact-store, returns S3 + Drive link. |
| `triage` | Read open signals from vault, group by severity, correlate with ClickHouse events, recommend actions per signal. |
| `enrich <arc-id>` | Semantic-search related sessions, append related-arcs + sources + decisions to the arc, recompute heat. |
| `replay <arc-id> --to <target>` | Read source arc's workflow, rewrite for new target, dispatch via `agenticore-run_task`, track replay edge. |
| `extract clusters --since <window>` | Retroactive cluster mining over a custom time window. |
| `dashboard` | Pull current state of all 27 brain dashboard panels, render as markdown summary, flag empty panels. |

Every non-tick command produces a markdown audit report and uploads it. Every command emits milestone/signal markers so `brain-tick` harvests the run next cycle (self-enhancement loop).

---

## Invocation paths

brain-keeper is reachable three ways, all through the same agenticore HTTP server:

### 1. LiteLLM model (`model: brain-keeper`)

Registered in LiteLLM prod (unit `brain-keeper`, model_id `82bdc994-ee97-444c-bc2f-29b5cf57f6cb`) via `add_agent_as_model` with `force=true`. Any consumer of the LiteLLM gateway can invoke it:

```bash
curl -X POST http://litellm.anton-prod.svc:4000/v1/chat/completions \
  -H "Authorization: Bearer $LITELLM_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"brain-keeper","messages":[{"role":"user","content":"run brain test"}]}'
```

**Semantics:** OpenAI path is stateless — no multi-turn memory through LiteLLM. Streaming is batched fake-streaming. `tools/tool_calls` in the request body are NOT forwarded to the agent subprocess (brain-keeper has its own MCP tool set from its profile).

### 2. AgentiBridge A2A dispatch

The canonical agent-to-agent path. Other agents (anton-agent, publisher, etc.) discover brain-keeper via `find_agents` / `list_agents` and dispatch via `run_agent`:

```python
run_agent(agent_id="brain-keeper-0", task="run brain test", wait=False)
```

- `wait=False` returns `job_id` immediately — poll job state via `/shared/job-state/{job_id}.json` on the pod or use `get_dispatch_job` from AgentiBridge.
- `wait=True` blocks up to **600s** (bumped from 30s — PR #36 merged to agentibridge/dev and live-patched on both anton-dev + anton-prod pods). Override with `AGENTIBRIDGE_AGENT_TIMEOUT` env var.

### 3. Cron tick

Vault hygiene `tick` command on a cron schedule (30 min). Runs deterministically without LLM orchestration for cheap housekeeping. Authoritative maintenance happens here; `test`/`triage`/etc. are operator-driven.

---

## Report pipeline — markdown + CSV → Google Drive

Every brain-keeper report is written in **two formats**, uploaded to artifact-store, and auto-mirrored to Google Drive where they're converted to native Google Docs and Google Sheets.

### Filename convention

```
brain-report-YYYY-MM-DD-HHMM.{md,csv}
```

UTC timestamp with minute granularity. Chronologically sortable in Drive, human-scannable, no opaque hashes. Runs within the same minute overwrite (acceptable — one per minute is fine).

Example: `brain-report-2026-04-13-2320.md` / `brain-report-2026-04-13-2320.csv`.

The internal 8-char hex run_id is still embedded in the report content and marker emissions for ClickHouse tracing — it just doesn't go in the filename.

### Upload path

```bash
# Markdown
curl -X PUT "http://10.10.30.130:8095/artifacts/miscellaneous/brain-keeper/${FILENAME_MD}?content_type=text%2Fmarkdown" \
  -H "Authorization: Bearer ${ARTIFACT_STORE_API_KEY}" \
  --data-binary @/tmp/${FILENAME_MD}

# CSV
curl -X PUT "http://10.10.30.130:8095/artifacts/miscellaneous/brain-keeper/${FILENAME_CSV}?content_type=text%2Fcsv" \
  -H "Authorization: Bearer ${ARTIFACT_STORE_API_KEY}" \
  --data-binary @/tmp/${FILENAME_CSV}
```

**Category:** `miscellaneous/brain-keeper/` — **NOT `system/test/*`**. The drive-sync Lambda explicitly skips anything under `system/*` (`handler.py:539`). `miscellaneous` is whitelisted in both `ALLOWED_PREFIXES` (artifact-store) and `CATEGORY_TO_DRIVE_FOLDER` (drive-sync), so both files mirror automatically.

**API key:** stored in OpenBao at `secret/k8s/agenticore` as `ARTIFACT_STORE_API_KEY`, synced via ESO into the `agenticore-secrets` K8s Secret, mounted as env on brain-keeper pods.

### Drive conversion — content-type routing

The drive-sync Lambda (`stacks/terraform/modules/s3-drive-connector/lambda/handler.py`) maps content-type to target Google services:

| Content-Type | Target | Result |
|---|---|---|
| `text/markdown` | `docs` | Native Google Doc (markdown rendered) |
| `text/csv` | `sheets` | Native Google Sheet (headers, rows, sortable) |
| `application/vnd.openxmlformats-officedocument.spreadsheetml.sheet` | `sheets` | Native Google Sheet |
| `text/html`, `application/pdf` | `docs` | Native Google Doc |
| `image/*`, `video/*` | `photos` | Google Photos upload |

`ENABLE_SHEETS=true` and `ENABLE_DOCS=true` are already set in Terraform (`stacks/terraform/s3_drive_sync.tf`). No env changes required.

### Drive location

Files land at: **Drive → `misc/brain-keeper/`** — operator's personal Drive root (via the connector's delegate-email subject), accessible from Drive web, Drive mobile app, or Sheets/Docs mobile apps.

### CSV schema

One row per check:

```csv
region,check,value,threshold,status,notes
Frontal Lobe,arcs_scanned,47,>0,PASS,Normal arc coverage
Frontal Lobe,heat_changes,0,>0,WARN,No heat changes this tick — arc state frozen
Broadcast Cortex,skip_throttle,692,<100,FAIL,77.8% suppressed by throttle
Hook Observability,brain_marker_write_p95_ms,762.6,<200,FAIL,4x above acceptable threshold
```

Phone-friendly: sortable by `region` or `status`, filterable for FAILs, chartable by time if multiple reports are consolidated.

---

## MCP tools assigned to brain-keeper

Per agent.yml `mcp_categories`:

- `tools-knowledge` — artifact_store (19), anton_jobs (7), atlassian (72, unused), obsidian (0 — MCP not wired, vault via NFS)
- `tools-notifications` — notifications (10)

Plus native Claude Code tools (Bash, Read, Write, Glob, Grep) and NFS-mounted `/vault` for Obsidian file I/O.

A curated **`brain-keeper-tools-prod`** LiteLLM unit exists with 93 tools (adds grafana, agentibridge, agenticore) but requires a new category entry in `agentihooks-bundle/.claude/.mcp.json` before the pod can consume it. That's a follow-up sprint.

---

## Validated end-to-end (2026-04-13)

Run `brain-report-2026-04-13-2320`:

1. Operator dispatched via AgentiBridge `run_agent(brain-keeper-0, "full brain test", wait=False)` from an external session
2. brain-keeper picked up the job, executed 3 curls against ClickHouse (`http://10.10.30.130:8123` with basic auth)
3. Built structured markdown + CSV reports with 6 region sections (Frontal Lobe, Amygdala, Broadcast Cortex, Hippocampus, Pineal, Hook Observability)
4. Uploaded both via REST to `miscellaneous/brain-keeper/` — returned presigned S3 URLs
5. Drive-sync Lambda mirrored both within ~60s and converted `.md` → Google Doc, `.csv` → Google Sheet
6. Operator opened Drive on mobile → `misc/brain-keeper/` → tapped both files, rendered cleanly

Cost: **~$0.15 per run** (Sonnet 4.6, ~40s wall time).

Findings from the run: brain self-diagnosed **DEGRADED** status — throttle suppression 77.8% (our own cadence fix was too aggressive), marker_write p95 762ms (4× threshold), 20+ noise single-session arcs. **The agent audited its own environment and called out the operator's own config decisions.** Self-enhancement loop confirmed.

---

## Readiness score

**7.5 / 10 — functional end-to-end, several follow-ups to harden**

### What works (8 items, ✓)

- ✓ brain-keeper pod healthy, auto-registered with AgentiBridge, heartbeats green
- ✓ LiteLLM model `brain-keeper` registered, routes correctly
- ✓ AgentiBridge A2A dispatch (sync + async), wait=true timeout bumped 30s → 600s
- ✓ Dual-format reports (markdown + CSV) with timestamped filenames
- ✓ Artifact-store REST upload path validated (`miscellaneous/brain-keeper/`)
- ✓ Drive sync Lambda auto-mirrors and converts to Google Doc + Google Sheet
- ✓ Self-enhancement loop: reports emit markers → brain-tick harvests → next run sees past lessons
- ✓ Broadcast cadence controls (dedup + throttle + cap) observable via brain.delivery spans

### Known issues / follow-ups (6 items)

1. **Sonnet, not Opus** — agent.yml says `model: opus` but `values.yaml` env `AGENT_MODE_MODEL=sonnet` at line 64 overrides. Needs value change + pod restart.
2. **Reports only in `/tmp`** — pod restart wipes them. Fix: upload to artifact-store is already the persistence path, but brain-keeper should also write to `/shared/brain-reports/` as an on-pod archive.
3. **Storage MCP fix not in image** — `hooks/mcp/storage.py` `storage_url` field fix shipped to agentihooks git but agenticore image still has the broken copy baked in. Live-patched, image rebuild pending (`gh run list` shows dev build queued).
4. **AgentiBridge wait timeout fix not in image** — live-patched on both prod + dev agentibridge pods, PR #36 merged to dev, image rebuild pending dev→main promotion.
5. **obsidian MCP has 0 tools** — server is FastAPI REST, not MCP-ified. brain-keeper uses NFS mount instead. Nice-to-have: MCP wrap the 4 endpoints (list_files, read_file, search_vault, write_inbox).
6. **Custom `brain-keeper-tools` unit not consumed** — 93-tool curated unit exists in LiteLLM (grafana + agentibridge + artifact_store + ...) but needs an entry in `agentihooks-bundle/.claude/.mcp.json` + a key env var injection to be usable. Would unlock native Grafana queries instead of raw curl.

### Opportunities

- **Telegram bridge:** operator wants "brain test" in Telegram to route to brain-keeper. Anton Agent's CLAUDE.md has the routing rule wired; needs Anton to actually call AgentiBridge `dispatch_to_agent capability=agent:brain-keeper` when users mention brain/vault/arc/signal/test topics.
- **Scheduled daily test:** add a cron entry in `workflows/crons/` that dispatches `brain-keeper test` daily at 06:00 UTC. Report lands in Drive before operator's first coffee.
- **Report index:** a `brain-report-index.md` in Drive that lists every run sorted by date — generated by the cron job, overwriting itself each run.
- **Loosen cadence:** brain-keeper's own report flagged the cadence fix as too aggressive. Raise `BROADCAST_MIN_INTERVAL_SEC` ceiling or `BROADCAST_MAX_PER_PROMPT` cap until throttle% drops below 50%.

---

## Files

| Path | Purpose |
|---|---|
| `agentihub/agents/brain-keeper/agent.yml` | Agent manifest (model, categories, concurrency) |
| `agentihub/agents/brain-keeper/package/system.md` | Command surface + upload pipeline spec |
| `agentihub/agents/brain-keeper/package/.agentihooks.json` | Channel subscriptions (brain, amygdala) |
| `antoncore/k8s/charts/brain-keeper/values.yaml` | StatefulSet config, env, MAX_PARALLEL_JOBS, ARTIFACT_STORE_URL |
| `antoncore/k8s/argocd/prod/brain-keeper.yaml` | ArgoCD app |
| `antoncore/stacks/artifact-store/src/main.py` | REST PUT /artifacts/{key} endpoint |
| `antoncore/stacks/terraform/modules/s3-drive-connector/lambda/handler.py` | Drive sync routing, `system/*` skip rule at line 539 |
| `agentihooks/hooks/mcp/storage.py` | storage_upload_path MCP tool (post-fix) |
| `agentibridge/agentibridge/registry.py` | `route_to_agent` with 600s `wait=True` timeout |
