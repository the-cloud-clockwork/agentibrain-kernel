---
name: brain-memory
description: Writes brain markers, ingests documents into the brain and searches it through the agentibrain MCP tools. Covers the marker attributes, where each marker lands in the vault, how markers reach brain-api, when to ingest instead of marking, and how to confirm content is searchable. Use when recording a lesson, decision, milestone or signal, persisting a design, research result or handoff, searching past work, or when the operator says "remember this", "write a lesson", "raise a signal", "ingest this", "search the brain" or "have we seen this before".
---

# Brain Memory

The brain profile's instructions already define what each marker type is for, show one example per type and list the brain tools. This skill adds the steps, the attributes, the vault destinations and the checks.

Pick the path by what you hold:

- One insight caught in flow → **Markers**.
- A document (design, research, investigation, handoff, reference) → **Ingest**.
- A question about past work, decisions or incidents → **Search**, before scanning the repository.

## Markers

1. Write the marker in your reply text: an opening comment `@TYPE` with its attributes and a closing comment `@/TYPE` with the same type. Done when both tags carry one of the four types and the content between them is non-empty.
2. Set the attributes from the table. Attributes are `key=value` or `key="value with spaces"`; `ts` is reserved and dropped. Done when every attribute the type routes on is set (`source` for a milestone that belongs to a project, `severity` for a signal).
3. Keep the content under 4096 characters and name the cause and the fix, the trade-off, the validated outcome or the observed evidence. Done when the content stands alone for a reader who never saw this session.

| Type | Attributes | Vault destination |
|---|---|---|
| `lesson` | `source` | appended to `left/reference/lessons-DATE.md`; the same content already in that day's log is kept once |
| `decision` | `title`, `date`, `source` | new `left/decisions/ADR-NNNN-SLUG.md` |
| `milestone` | `status` (default `done`), `scope`, `source` | `left/projects/SOURCE/BLOCKS.md` when that project folder exists, else `daily/DATE.md` |
| `signal` | `severity`: `nuclear`, `critical`, `warning`, `info` or `resolved` (anything else becomes `warning`); `source`; `title` | new `amygdala/STAMP-SEVERITY-SLUG.md`; an open signal with the same content, severity and source absorbs a repeat |

### How a marker reaches the brain

- The agentihooks brain writer runs at every Stop. It reads your reply text from the session transcript (tool inputs, files and thinking are not read), finds each marker pair, adds `session_id` and a default `source`, and POSTs it to brain-api `/marker`, which writes the vault destination above.
- A replay of the same session, type and content within an hour returns the first write; attributes are not part of that key. Beyond that, only the per type vault rule in the table prevents a duplicate.
- When brain-api is unreachable the marker waits in the agentihooks brain outbox and is sent at the next Stop.
- A marker quoted in your reply as an example is captured like a real one. To show the format in a reply, describe it in words instead of writing the tags.

To check a marker landed, after the turn that wrote it has ended: `mcp__agentibrain__vault_read` its destination, or run **Ingest** steps 3 and 4 to make it searchable now.

## Ingest

1. Call `mcp__agentibrain__brain_ingest` with `content` and a `title`; keep the default `producer` unless the source is a named service. Content lands in the raw inbox and is chunked when large. Done when `chunks_sent` equals `total_chunks`.
2. When nothing needs the content today, stop here: the scheduled tick assimilates it. Done when you have said the content is ingested and not yet searchable.
3. When the content must be searchable now, call `mcp__agentibrain__brain_tick`; pass `no_ai=true` when you only added markers. Done when the status is `completed`; a `pending` status means the tick still runs server side, so call it again before step 4.
4. Call `mcp__agentibrain__kb_search` with a distinctive phrase from the content. Done when a hit carries it; until then, report the content as ingested but not yet confirmed searchable.

## Search

1. Query with the symptom, service or decision in plain words: `mcp__agentibrain__kb_search` for the whole knowledge base (`producer="brain-lesson"` or `"brain-arc"` narrows it), `mcp__agentibrain__brain_search_arcs` for past units of work like the current one, `mcp__agentibrain__kb_brief` for a short synthesized brief over the top hits instead of raw results. Done when you hold the ranked hits or an empty result.
2. Follow the best hit: `mcp__agentibrain__brain_get_arc` with its `cluster_id`, or `mcp__agentibrain__vault_read` with its path (`mcp__agentibrain__vault_list` with a prefix finds one: `left/reference` for lesson logs, `amygdala` for signals, `brain-feed` for the session feed). Done when you have read the full arc or document behind each hit you will cite.
3. Verify any code fact it names against the working tree; the brain holds intent and history, the tree holds current code. Done when the answer cites the hits it rests on, or states that the brain returned nothing relevant.
