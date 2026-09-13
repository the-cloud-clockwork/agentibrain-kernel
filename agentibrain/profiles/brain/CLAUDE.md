# Brain Usage Instructions

Every agent that reads this configuration has access to the brain system: persistent
memory shared across agents, fleets, workforces, and workspaces. It outlives this
session. Use it. Emitting markers alone is not sufficient.

What you write may be read weeks later against a codebase that has moved. Name
types, functions, services, commands, and behavior rather than file paths or line
numbers. Attach evidence to claims that depend on a run, deployment, or commit.

## Required Usage (HARD RULE)

- Use brain tools for every explicit brain read and durable document write. Markers
  are the automatic write path for atomic insights. Never inspect or modify the vault
  with shell commands, local filesystem tools, curl, generic retrieval or
  recommendation tools, or another MCP server.
- Before answering a question or scanning code and directories, check whether the
  required context exists in the current context window. If it does not, query the
  brain first for applicable lessons, decisions, incidents, and prior work. The brain
  is the authority for fleet memory and historical intent; inspect the working tree
  afterward only to verify current implementation and possible drift. Never use a
  repository scan as a substitute for the required brain lookup.
- Search the brain when the operator asks about past work, before an architectural
  decision, when a bug feels familiar, or when entering a service not examined in
  this session. Use the working tree for current code facts and the brain for
  historical intent, decisions, incidents, and lessons.
- Ingest any important, durable detail that will matter beyond the current session:
  research, designs, investigation results, operational knowledge, handoffs, and
  reference material. `brain_ingest` is the document path; markers are the atomic
  insight path.
- After ingestion, call `brain_tick` when the current task needs the content
  searchable now. Wait for a completed tick and verify it with a brain search. If
  immediate retrieval is unnecessary, let the scheduled tick process it.
- A missing brain capability is a missing tool. Add or repair the tool; never bypass
  the brain by reaching into its storage.

## Brain Tools

| Need | Tool |
|---|---|
| Current injected state, recent lessons, active signals, operator intent | `brain_feed` |
| Search the full knowledge base | `kb_search` |
| Synthesize search results into a short brief | `kb_brief` |
| Find related past work, decisions, or incidents | `brain_search_arcs` |
| Read one complete arc by `cluster_id` | `brain_get_arc` |
| Find an exact vault path through the brain API | `vault_list` |
| Read a known vault document through the brain API | `vault_read` |
| Persist a document, synthesis, design, or reference | `brain_ingest` |
| Queue processing and index refresh | `brain_tick` |

`vault_list` and `vault_read` are brain tools. They are the only valid way to inspect
known vault documents. Do not translate their paths into local filesystem commands.

`brain_ingest` writes content to the raw inbox. A full tick assimilates it by
classifying, clustering, summarizing, organizing, and indexing it. `brain_tick` is
queued work, not an instantaneous write: it may return `pending`. Content is not
confirmed searchable until the tick completes and a brain query returns it.

Use `brain_status` to diagnose the AgentiHooks brain adapter and `brain_refresh` to
republish the current feed after its source changes. These bridge tools do not replace
`brain_tick`: refresh republishes existing feed state; tick processes and indexes new
knowledge.

## Markers

HTML comments in your output are captured automatically. Use them for atomic insights
caught in flow; use `brain_ingest` for documents and substantial durable context.

```markdown
<!-- @lesson -->
psycopg2 connections are not thread-safe — use ThreadedConnectionPool.
<!-- @/lesson -->

<!-- @decision date=2026-07-21 -->
Fight/flight stays an operator call. The brain raises a banner; agents never auto-halt.
<!-- @/decision -->

<!-- @milestone status=done scope=brain -->
Signal tombstones shipped — resolved signals clear on the next pass.
<!-- @/milestone -->

<!-- @signal severity=warning source=deploy -->
publisher-0 restarted 3 times in 10 minutes after the image bump.
<!-- @/signal -->
```

- `@lesson` — a fix that took real investigation, or a pattern that saves the
  next person time. Not the obvious, not the already-documented.
- `@decision` — an architectural choice nobody should relitigate. Record the
  trade-off and why the alternative lost; the reasoning is the value.
- `@milestone` — a complete, validated unit of work. Not a commit.
- `@signal` — needs attention. Severities `nuclear` · `critical` · `warning` ·
  `info` · `resolved`; pick honestly. A credential exposed anywhere is `nuclear`,
  `source=security`, immediately.

Name the cause and the fix. One insight per marker, five per session maximum, and two
good markers beat five weak ones.

## Session Context and Channels

At session start, the AgentiHooks brain adapter reads the current feed and publishes
its entries as `BROADCAST` blocks. The feed can contain hot arcs, recent lessons,
operator intent, inject blocks, tick changes, and active signals. An **arc** is a past
unit of work with its timeline, lessons, and links to related arcs. Hot arcs are the
recent or frequently referenced subset; use brain tools to retrieve the rest.

You subscribe to `brain` for state and `amygdala` for emergencies. Use
`channel_publish` only for live coordination, and use it rarely because every
broadcast spends fleet attention. Knowledge belongs in markers or `brain_ingest`.
Markers persist automatically; signals reach session broadcasts after brain processing
and feed refresh. Milestones are persisted but are not immediate broadcasts.

The absence of a `BROADCAST` block does not prove the session is unsubscribed. The feed
may be empty, unchanged, disabled, or unavailable. Use `brain_status` to distinguish
those states.

AgentiHooks reconciles the brain feed every 20 tool calls by default, controlled by
`BRAIN_REFRESH_TOOL_CALLS`, and injects new or restored entries into that tool call. It
injects up to 10 hot arcs by default, controlled by `BRAIN_HOT_ARCS_TOP_N`, and caps
each feed entry at 1,536 characters by default through `BRAIN_PAYLOAD_MAX_BYTES`.

## Constraints (HARD FLOOR)

All brain access goes through brain tools and the HTTP contracts behind them. No direct
vault reads, direct vault writes, or storage-path guesses. Brain behavior changes go
through code, commit, CI, deployment, and validation. The brain decides where ingested
content belongs; agents do not choose or manipulate its storage region.
