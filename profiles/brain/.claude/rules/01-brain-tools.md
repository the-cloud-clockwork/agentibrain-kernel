# Brain Tools — Read, Write, Search (Priority 1)

You are connected to the Brain Nervous System via MCP. The brain is the fleet's shared memory — arcs, lessons, signals, and operator knowledge stored in a vault and indexed in pgvector. You can read it, search it, and write to it.

## Tool Inventory

All tools live on the `agentibrain` MCP server. Your session sees them prefixed by your MCP proxy path — call them by matching the tool name suffix.

### Read Tools

| Tool | Purpose | When to use |
|------|---------|-------------|
| `kb_search` | Federated search across embeddings (semantic) + vault (text). Returns scored hits with previews. | First choice for any knowledge lookup. "What do we know about X?" |
| `kb_brief` | Runs `kb_search` then synthesizes a 3-5 line brief via LLM. Returns brief + candidate refs. | When you need a summary, not raw hits. "Brief me on X." |
| `brain_search_arcs` | Semantic search over brain arcs (narrative work units). Filters by heat and score. | When looking for past work sessions, decisions, or project history. "What did we do about X?" |
| `brain_get_arc` | Fetch full text + metadata for one arc by cluster_id. | Follow-up after `brain_search_arcs` — drill into a specific arc. |

### Write Tools

| Tool | Purpose | When to use |
|------|---------|-------------|
| `brain_ingest` | Write text directly to the brain vault. Content lands in raw/inbox/, gets classified and moved to a region dir by the next tick (≤2 min). | When you have knowledge worth preserving: architecture docs, research findings, synthesis of a complex task, reference material. |

### Hooks-Utils Tools (local session)

| Tool | Purpose |
|------|---------|
| `brain_status` | Current brain adapter state: source, entry count, channel. |
| `brain_refresh` | Force the brain adapter to re-read and republish to the brain channel. |
| `channel_publish` | Publish a message to any broadcast channel (brain, ops-alerts, etc). |

## Auto-Lookup Rules

**Search the brain BEFORE answering** when:
- The operator asks about a past project, decision, or incident
- You encounter a system/service you haven't seen in this session's context
- The operator says "what did we do about", "how did we handle", "remind me"
- You're about to make an architectural decision — check if a prior `@decision` exists
- You're debugging something that feels like it happened before

**Do NOT search** when:
- The answer is in the current conversation context or CLAUDE.md
- The question is about code that's in the working tree (read the file instead)
- The operator is giving you instructions, not asking questions

**Pattern:**
```
1. kb_search("the topic") — get hits
2. If a hit looks relevant → brain_get_arc(cluster_id) for full context
3. If you need synthesis → kb_brief("the question") instead of steps 1-2
```

## Auto-Write Rules

**Ingest to the brain** when:
- You produce a synthesis or reference document the operator would want later
- The operator says "save this", "remember this", "write this to the brain"
- You complete a significant research task with findings worth preserving

**Use markers instead** (see `02-brain-markers.md`) when:
- You learn a non-obvious lesson during work (@lesson)
- A deliverable ships (@milestone)
- Something is broken or at risk (@signal)
- An architectural choice is made (@decision)

Markers are for atomic insights captured in-flow. `brain_ingest` is for deliberate, larger knowledge dumps.

## brain_ingest Usage

```
brain_ingest(
  content="# Title\n\nThe content...",
  title="short-descriptive-title",
  producer="agent"        # or your agent name
)
```

- Content is auto-chunked at 200k chars for large documents
- Title becomes the vault filename slug
- Tags are auto-extracted by the classification LLM
- The tick drains inbox every ≤2 minutes and moves notes to the appropriate vault region (left/ for technical, right/ for creative/strategic, bridge/ for cross-cutting)
