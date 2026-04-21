# Brain Markers — What to Emit and When (Priority 1)

You are connected to the Brain Nervous System. Your output is scanned for HTML comment markers that feed back into the brain in real-time. Use them when the moment calls for it — not every turn, not never. The hooks handle the rest.

## Marker Types

### @lesson — You learned something non-obvious
```markdown
<!-- @lesson -->
NFS dirs created by root need chmod 777 for UID 1000 writers — the error is silent until a different process tries to write.
<!-- @/lesson -->
```
**When:** A fix that took investigation. A pattern that would save time next time. Something that isn't in the docs.
**Not for:** Obvious things. Things already documented. Restating what the operator told you.

### @milestone — A block, task, or deliverable is done
```markdown
<!-- @milestone status=done scope=brain-system -->
Signal tombstone logic shipped. Resolved signals auto-removed next tick.
<!-- @/milestone -->
```
**When:** A meaningful unit of work is complete and validated. Blocks, features, deployments.
**Not for:** Individual commits. Intermediate steps. Partial progress.

### @signal — Something is broken, at risk, or needs attention
```markdown
<!-- @signal severity=warning source=deploy -->
publisher-0 restarted 3 times in 10 minutes after image update.
<!-- @/signal -->
```
**Severity levels:**
- `nuclear` — production down, data loss, security breach. Fleet halts non-critical work.
- `critical` — service degraded, auth failing, pipeline broken. Operator attention needed.
- `warning` — something is off but not broken yet. Monitor.
- `info` — FYI. Logged but no alert.
- `resolved` — previously raised signal is now fixed.

**SECURITY RULE:** If you detect a credential, API key, token, or password exposed in logs, output, git history, or any public surface — emit `severity=nuclear source=security` IMMEDIATELY. Do not wait. Do not ask. This is the one signal that overrides everything.

### @decision — An architectural or design choice was made
```markdown
<!-- @decision date=2026-04-11 -->
Fight/flight is operator decision. Amygdala shows banner, agents never auto-halt.
<!-- @/decision -->
```
**When:** A choice that future sessions should know about. Trade-offs. Why X over Y.
**Not for:** Implementation details. Config values. Things visible in the code.

## Rules

1. **Markers are invisible in rendered markdown** — they're HTML comments. The operator and other humans won't see them unless viewing raw source. Write them freely.
2. **One marker per insight** — don't bundle multiple lessons into one block.
3. **Be specific** — "fixed the bug" is useless. "psycopg2 connections are not thread-safe, replaced with ThreadedConnectionPool" is a lesson.
4. **Don't force it** — if a turn has nothing worth marking, emit nothing. Quality over quantity.
5. **The hooks do the rest** — you don't need to "send" markers anywhere. Just write them in your output. The brain_writer_hook scans, parses, and routes automatically.
6. **Max 5 markers per session** — if you've emitted 5, stop. The brain has enough for this session.
