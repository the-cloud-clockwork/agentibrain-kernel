---
name: brain-memory
description: Writes brain markers, ingests documents into the brain and searches it through the agentibrain MCP tools. Covers the marker format, the four marker types and their attributes, where each marker lands in the vault, when to ingest instead of marking, and how to confirm content is searchable. Use when recording a lesson, decision, milestone or signal, persisting a design, research result or handoff, searching past work, or when the operator says "remember this", "write a lesson", "raise a signal", "ingest this", "search the brain" or "have we seen this before".
---

# Brain Memory

Pick the path by what you hold:

- One insight caught in flow (a fix, a choice, a finished unit, a hazard) → **Markers**.
- A document (design, research, investigation, handoff, reference) → **Ingest**.
- A question about past work, decisions or incidents → **Search**, before scanning the repository.

Every read and write goes through the brain tools below or a marker. Reading or writing the vault with a shell, curl, a file tool or another MCP server is out; a missing capability is a missing tool to add in the kernel.

Write for a reader weeks away against a moved codebase: name types, functions, services, commands and behaviour, never file paths or line numbers, and attach the run, commit or command output a claim depends on.

## Markers

1. Write the marker in your reply text, as an HTML comment pair whose closing tag repeats the type:

   ```markdown
   <!-- @signal severity=critical source=deploy title="publisher crash loop" -->
   publisher-0 restarted 3 times in 10 minutes after the image bump; logs show the missing BRAIN_URL setting.
   <!-- @/signal -->
   ```

   Attributes are `key=value` or `key="value with spaces"`. Content is at most 4096 characters. `ts` is reserved and dropped.
2. Pick the type and its attributes from the table. One insight per marker, five per session at most.
3. Name the cause and the fix (lesson), the trade-off and why the alternative lost (decision), the validated outcome (milestone), or the observed evidence (signal).

Done when the reply carries the opening and closing tags with one of the four types and non-empty content.

| Type | Use for | Attributes | Vault destination |
|---|---|---|---|
| `lesson` | a fix that took real investigation, a pattern that saves the next agent time | `source` | appended to `left/reference/lessons-DATE.md`; the same content twice is kept once |
| `decision` | an architectural choice nobody should relitigate | `title`, `date`, `source` | new `left/decisions/ADR-NNNN-SLUG.md` |
| `milestone` | a complete, validated unit of work, never a commit | `status` (default `done`), `scope`, `source` | `left/projects/SOURCE/BLOCKS.md` when that project folder exists, else `daily/DATE.md` |
| `signal` | something that needs attention now | `severity`: `nuclear`, `critical`, `warning`, `info` or `resolved` (anything else becomes `warning`); `source`; `title` | new `amygdala/STAMP-SEVERITY-SLUG.md`; an open signal with the same content absorbs a repeat |

A credential exposed anywhere is `severity=nuclear source=security`, at once. Close a signal you fixed with a `resolved` signal for the same claim.

### How a marker reaches the brain

- The agentihooks brain writer runs at every Stop. It reads your reply text from the session transcript (tool inputs, files and thinking are not read), finds each marker pair and POSTs it to brain-api `/marker`, which writes the vault destination above.
- Repeats are idempotent: the same type, content and attributes return the first write.
- When brain-api is unreachable the marker waits in the agentihooks brain outbox and is sent at the next Stop.
- A marker quoted in your reply as an example is captured like a real one. To show the format in a reply, describe it instead of writing the tags.
- The next brain tick classifies the new entries, embeds lessons for search and refreshes the feed that sessions receive.

To check a marker landed, after the turn that wrote it has ended: `mcp__agentibrain__vault_read` its destination, or run **Ingest** step 3 to make it searchable now.

## Ingest

1. Call `mcp__agentibrain__brain_ingest` with `content` and a `title`; keep the default `producer` unless the source is a named service. Content lands in the raw inbox and is chunked when large. Done when `chunks_sent` equals `total_chunks`.
2. When nothing needs the content today, stop here: the scheduled tick assimilates it.
3. When the content must be searchable now, call `mcp__agentibrain__brain_tick`. Pass `no_ai=true` when you only added markers; keep the full tick for new documents. A `pending` status means the tick is still running server side: call `mcp__agentibrain__brain_tick` again or search again later, and treat the content as not yet searchable meanwhile.
4. Confirm with `mcp__agentibrain__kb_search` on a distinctive phrase from the content. Done when a hit carries it; until then, say the content is ingested but not yet confirmed searchable.

## Search

| Need | Tool |
|---|---|
| What this session was fed: hot arcs, recent lessons, active signals, operator intent | `mcp__agentibrain__brain_feed` |
| Federated search over embeddings and vault text; `producer="brain-lesson"` or `"brain-arc"` narrows it | `mcp__agentibrain__kb_search` |
| A short synthesized brief over the top hits | `mcp__agentibrain__kb_brief` |
| Past units of work similar to the current one | `mcp__agentibrain__brain_search_arcs`, then `mcp__agentibrain__brain_get_arc` with its `cluster_id` |
| An exact vault document: lesson logs under `left/reference`, signals under `amygdala`, the feed under `brain-feed` | `mcp__agentibrain__vault_list` with a prefix, then `mcp__agentibrain__vault_read` |

1. Query with the symptom, service or decision in plain words.
2. Follow the best hit to its full arc or document.
3. Verify any code fact it names against the working tree; the brain holds intent and history, the tree holds current code.

Done when the answer cites the hits it rests on, or states that the brain returned nothing relevant.
