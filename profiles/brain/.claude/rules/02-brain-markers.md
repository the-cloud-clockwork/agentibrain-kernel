# Brain Markers — Passive Write Path (Priority 2)

Your output is scanned for HTML comment markers that feed back into the brain in real-time. The `brain_writer_hook` parses them and POSTs to brain-api `/marker`. You just write them — the hooks handle routing.

## Marker Types

### @lesson — Non-obvious learning
```markdown
<!-- @lesson -->
psycopg2 connections are not thread-safe — replaced with ThreadedConnectionPool.
<!-- @/lesson -->
```
**When:** A fix that took investigation. A pattern that saves time next time.
**Not for:** Obvious things. Things already documented.

### @milestone — Deliverable shipped
```markdown
<!-- @milestone status=done scope=brain-system -->
Signal tombstone logic shipped. Resolved signals auto-removed next tick.
<!-- @/milestone -->
```
**When:** A meaningful unit of work is complete and validated.
**Not for:** Individual commits. Intermediate steps.

### @signal — Something needs attention
```markdown
<!-- @signal severity=warning source=deploy -->
publisher-0 restarted 3 times in 10 minutes after image update.
<!-- @/signal -->
```
**Severities:** `nuclear` (prod down, data loss, security) → `critical` (degraded) → `warning` (off but not broken) → `info` (FYI) → `resolved` (fixed).

**SECURITY:** Credential/key/token exposed anywhere → emit `severity=nuclear source=security` IMMEDIATELY.

### @decision — Architectural choice
```markdown
<!-- @decision date=2026-04-11 -->
Fight/flight is operator decision. Amygdala shows banner, agents never auto-halt.
<!-- @/decision -->
```
**When:** A choice future sessions should know about. Trade-offs. Why X over Y.

## Rules

1. Markers are HTML comments — invisible in rendered markdown. Write them freely.
2. One marker per insight.
3. Be specific — "fixed the bug" is useless.
4. Don't force it — no marker is better than a low-quality one.
5. Max 5 markers per session.

## Markers vs brain_ingest

| | Markers | brain_ingest |
|---|---------|-------------|
| **Size** | 1-3 sentences | Paragraphs to full documents |
| **When** | In-flow, as you work | Deliberate knowledge dump |
| **How** | Write HTML comment in output | Call MCP tool |
| **Processing** | brain_writer_hook → POST /marker → next tick | POST /ingest → raw/inbox/ → next tick |
| **Use for** | Lessons, milestones, signals, decisions | Architecture docs, research, references |
