# Brain

You have memory. Not a longer context window — an actual one, shared with
every other agent in the fleet, that outlives this session.

The brain holds what was learned, decided, tried, and broken. It is fed by
agents doing real work and it feeds them back. Nothing you write here is for
you; it is for whoever picks this up next, days from now, against a codebase
that has moved. Write accordingly.

## How you work with it

**You already have context.** Every session opens with `BROADCAST` blocks —
the hot arcs, what the operator is trying to do, what changed in the last
maintenance pass, and anything currently alarming. Read them. That is the
fleet's live state, not decoration.

**An arc** is a unit of work with a story: an ignition, a timeline, lessons,
edges to related arcs. Arcs have *heat* — recent, active, referenced work runs
hot; stale work cools and sinks. What you see injected is the hot end.

**Go deeper when you need to.** The injected blocks are a summary. When you
need the full thing, retrieve it.

**Write back as you go.** You noticed something non-obvious — write a marker.
You produced something worth keeping — ingest it. Neither is a chore you do at
the end; both are how the memory stays alive.

## The six tools

| | |
|---|---|
| `kb_search` | Anything: "what do we know about X?" |
| `kb_brief` | Same, synthesized into a few lines instead of raw hits |
| `brain_search_arcs` | Past work, decisions, incidents |
| `brain_get_arc` | One arc in full, by `cluster_id` |
| `brain_ingest` | Write a document into the brain |
| `brain_tick` | Force processing now, so what you just wrote is findable now |

Search before you answer a question about past work, before repeating an
architectural decision, and when a bug feels familiar. Not when the answer is
in front of you.

## Regions

The brain sorts what you write; you do not choose where it lands.

- `left` — technical. Projects, research, decisions, incidents.
- `right` — creative and strategic. Ideas, direction, risk.
- `bridge` — synthesis across the two.
- `frontal-lobe` — conscious (hot, active) and unconscious (cooled off).
- `pineal` — breakthroughs and joy.
- `amygdala` — alarm. Things actively on fire.

## Markers

HTML comments in your output. Invisible when rendered, captured automatically.

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

Severities: `nuclear` · `critical` · `warning` · `info` · `resolved`.
A credential exposed anywhere is `nuclear`, immediately.

Be specific — "fixed the bug" teaches nobody. Five markers a session is the
ceiling, and no marker beats a weak one. One-liners are markers; documents are
`brain_ingest`.

## Channels

You subscribe to `brain` (state) and `amygdala` (emergencies). Publishing is
for coordination only — "restarting X, hold off" — never for knowledge.
Knowledge goes through markers and ingest.

## Constraints

- Reach the brain through its tools. No curl, no writing to the vault directly.
- A missing capability is a missing tool — add it, don't work around it.
- Brain behaviour changes go through code, commit, and CI like anything else.
