# Brain Tools — When To Reach For Them

The six tools are listed in `CLAUDE.md`. This is the discipline for using them.

## Search before you answer

- The operator asks about a past project, decision, or incident.
- "What did we do about…", "how did we handle…", "remind me…".
- You are about to make an architectural call — check for a prior `@decision`.
- A bug feels familiar.
- You hit a service or system you have not seen this session.

## Don't search

- The answer is already in this conversation or in `CLAUDE.md`.
- The question is about code in the working tree — read the file.
- The operator is instructing you, not asking you.

## Pattern

```
kb_search("topic")                 → hits
brain_get_arc(cluster_id)          → full context on a promising hit
kb_brief("question")               → when you want the synthesis, not the hits
```

## Writing

`brain_ingest(content=..., title=..., producer=...)` — a title becomes the
filename slug; tags and destination are worked out for you. Use it when you
produced something durable: a synthesis, a design, reference material.

Use markers instead for atomic insights caught in flow — see
`02-brain-markers.md`.

New content is not searchable until it is processed. `brain_tick()` forces
that immediately; otherwise it happens on the next scheduled pass.
