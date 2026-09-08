# Brain

Persistent memory shared across every agent in the fleet, outliving this
session. What you write is read weeks later by an agent working a codebase that
has moved — name types, functions, services, commands, not file paths.

Sessions open with `BROADCAST` blocks carrying the fleet's live state: hot arcs,
operator intent, recent changes, active alarms. An **arc** is a past unit of
work — ignition, timeline, lessons, links to related arcs. Hot means recent or
frequently referenced; the injected blocks are the hot end and a summary.
Retrieve the rest when you need it.

## Tools

| | |
|---|---|
| `kb_search` | "what do we know about X?" |
| `kb_brief` | same, synthesized instead of raw hits |
| `brain_search_arcs` | past work, decisions, incidents |
| `brain_get_arc` | one arc in full, by `cluster_id` |
| `brain_ingest` | write a document (a synthesis, a design, reference material) |
| `brain_tick` | process now, so what you just wrote is findable now |

Search when: the operator asks about past work; you are about to make an
architectural call (check for a prior `@decision`); a bug feels familiar; you
hit a service you have not seen this session. Do not search when the answer is
in this conversation, in `CLAUDE.md`, or in the working tree.

## Markers

HTML comments in your output, invisible when rendered, captured automatically.
Use them for atomic insights caught in flow; use `brain_ingest` for documents.

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
  `info` · `resolved`; pick honestly, an inflated `critical` costs the whole
  fleet's attention. A credential exposed anywhere is `nuclear`,
  `source=security`, immediately.

Name the cause and the fix — "fixed the bug" teaches nobody. One insight per
marker, five per session maximum, and two good ones beat five weak ones.

## Channels

You subscribe to `brain` (state) and `amygdala` (emergencies). Publish only to
coordinate — `channel_publish(channel="brain", message="restarting litellm-0,
hold off on MCP calls", severity="info")` — and rarely; every broadcast spends
attention across every agent. Knowledge goes to markers and `brain_ingest`.
Milestones and signals already broadcast themselves. No `BROADCAST` blocks at
all means this session is not subscribed.

## Constraints

Reach the brain through its tools — no curl, no writing to the vault directly.
A missing capability is a missing tool: add it. Brain behaviour changes go
through code, commit, and CI. The brain sorts what you write into regions; you
do not choose where it lands.
