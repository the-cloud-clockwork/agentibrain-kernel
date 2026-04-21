---
title: Brain Markup Protocol — Marker Specification
status: living
date: 2026-04-10
related:
  - docs/brain/CLUSTERS.md
  - docs/brain/ARCHITECTURE.md
  - stacks/brain-tools/markers.py
---

# Brain Markup Protocol — Marker Specification

> **Parser:** `stacks/brain-tools/markers.py` (pure stdlib Python, self-test included)
> **Design principle:** 80% deterministic, 20% AI. Markers enable the 80%.

---

## 1. Why Markers

Documents in the brain vault contain structured information (lessons, signals, decisions, edges) buried in prose. Without markers, extracting that information requires an LLM to read and interpret every document every time — slow ($2.16/tick) and unreliable (80 turns, FAILED).

Markers are HTML comments embedded in markdown. They serve two audiences:
1. **Regex/Python** — `grep`, `re.findall()`, `markers.py` extract structured data in milliseconds
2. **LLMs** — when they read the document, markers are semantic anchors that compress understanding

Markers are **invisible in Obsidian and GitHub** (HTML comments don't render). The vault reading experience is clean. The parsing experience is fast.

---

## 2. Syntax

### Inline markers (single-line, no content)
```
<!-- @type key=value key="quoted value" -->
```

### Block markers (multi-line, with content)
```
<!-- @type key=value -->
Content that belongs to this marker.
Can span multiple lines.
<!-- @/type -->
```

### Regex patterns
```python
# Inline: captures (type, attributes_string)
r'<!--\s*@(\w+)((?:\s+\w+=[^\s>]+|\s+\w+="[^"]*")*)\s*-->'

# Block: captures (type, attributes_string, content)
r'<!--\s*@(\w+)((?:\s+\w+=[^\s>]+|\s+\w+="[^"]*")*)\s*-->(.*?)<!--\s*@/\1\s*-->'

# Attributes: captures (key, quoted_value, unquoted_value)
r'(\w+)=(?:"([^"]*)"|(\S+))'
```

---

## 3. Marker Types

### @hot — Hot spots
Content the operator or brain-keeper considers immediately important. Extracted for agent context injection.

```markdown
<!-- @hot heat=9 region=left -->
This paragraph is a hot spot — deterministic parsers extract it.
<!-- @/hot -->
```

**Attributes:** `heat` (int, 0-10), `region` (left/right/bridge/amygdala/pineal)

### @lesson — Lessons learned
Reproducible insights extracted from arc resolutions. The brain's long-term learning.

```markdown
<!-- @lesson -->
Always write LITELLM_KEY unconditionally — silent failures are the worst.
<!-- @/lesson -->
```

**Attributes:** none required. Content is the lesson.

### @signal — Active signals
Conditions that need monitoring. Amygdala candidates when severity is critical/nuclear.

```markdown
<!-- @signal severity=critical source=auth-broker verify="kubectl -n auth-broker get pods | grep -q Running" -->
Multi-account Claude Max is a single point of failure for the fleet.
<!-- @/signal -->
```

**Attributes:**
- `severity` (required) — `info` | `warning` | `critical` | `nuclear` | `resolved`. Severity order: info < warning < critical < nuclear.
- `source` (SHOULD) — kebab-case slug identifying the producing system (e.g. `argocd-image-updater`, `paper2slides-s3`). The apply phase (`brain_apply.py`) matches signals to AI `ESCALATE`/`CLEAR` directives by this attribute. Signals without `source=` fall through to a fuzzy content-match — less reliable.
- `verify` (SHOULD) — a shell command that returns exit 0 when the signal's claim is FALSE (i.e. the underlying issue is resolved). On every tick, `brain_verifier.verify_all()` runs this command. If it exits 0, the signal is auto-tombstoned and drops out of `signals.md` for that tick. Timeouts default to 10s and never block the tick.

**Examples of verify commands:**
- Secret exists: `kubectl -n argocd get secret argocd-image-updater-ghcr -o json | jq -e '.data.credentials'`
- Pod healthy: `kubectl -n foo get pod bar -o jsonpath='{.status.phase}' | grep -q Running`
- HTTP endpoint up: `curl -sf http://service.ns.svc:8080/health`
- Zero error logs in last 5m: `! kubectl logs -n foo bar --since=5m | grep -qi error`

**Rule of thumb:** if a signal is worth broadcasting fleet-wide, it is worth attaching a one-liner that proves it. An unverifiable signal erodes trust across every agent session.

### @decision — Decisions made
Architectural or operational decisions worth remembering. Prevents re-litigating.

```markdown
<!-- @decision date=2026-04-10 -->
Use deterministic parsers over LLM agents for structured data extraction.
<!-- @/decision -->
```

**Attributes:** `date` (ISO date)

### @edge — Arc relationships
Links between arcs. The graph structure of the brain.

```markdown
<!-- @edge type=parent target=2026-04-09-artifact-platform -->
<!-- @edge type=unblocks target=2026-04-09-brain-etl-arc-system -->
```

**Attributes:** `type` (parent/child/sibling/unblocks/supersedes/related), `target` (cluster_id)
**Note:** Inline-only (no block form). Multiple edges per document.

### @inject — Context injection candidates
Content that should be injected into agent CLAUDE.md or SessionStart context. brain_keeper.py collects these into `brain-feed/inject.md`.

```markdown
<!-- @inject target=claude.md -->
Brain MVP is the top priority. Agent Prompt RL sequenced after.
<!-- @/inject -->
```

**Attributes:** `target` (claude.md/all/specific-agent)

### @todo — Tracked tasks
Items that need doing. brain_keeper.py can collect and surface these.

```markdown
<!-- @todo priority=1 -->
First operator-profile corpus run — 687 sessions.
<!-- @/todo -->
```

**Attributes:** `priority` (int, 1=highest)

---

## 4. YAML Frontmatter

Every arc file starts with YAML frontmatter between `---` delimiters. This is the document-level metadata. Markers are body-level (inline) metadata.

```yaml
---
cluster_id: 2026-04-09-litellm-mcp-self-service
title: LiteLLM MCP Self-Service Arc
region: left-hemisphere
status: complete
heat: 9
source_sessions:
  - 7d031027
  - a69e27d7
synthesized: true
created: 2026-04-09
---
```

**Frontmatter is parsed by `markers.parse_frontmatter()`** — simple key: value lines, no nested YAML. Lists supported (YAML `- item` syntax or `[item, item]` inline).

---

## 5. How brain_keeper.py Uses Markers

| Marker | brain_keeper.py action |
|--------|----------------------|
| Frontmatter `heat` | Recomputed every tick, updated in-place if changed |
| Frontmatter `status` | Determines promotion/demotion eligibility |
| `@hot` | Collected for potential brain-feed injection |
| `@lesson` | Counted in tick stats, available for search/aggregation |
| `@signal` | Collected into `brain-feed/signals.md` |
| `@inject` | Collected into `brain-feed/inject.md` |
| `@edge` | Used by AI reasoning layer for graph analysis |
| `@decision` | Not actively processed yet — future: surface in operator briefings |
| `@todo` | Not actively processed yet — future: surface in operator TODO |

---

## 6. How the AI Reasoning Layer Uses Markers

The AI tick (`brain_tick_prompt.py`) receives pre-extracted marker data as compressed context. It does NOT read files. It reasons about:

- **Missing edges** — "These two arcs should be linked but have no @edge marker"
- **Signal escalation** — "This @signal should be critical, not warning"
- **Signal clearing** — "This @signal was fixed, mark as resolved"
- **Merge candidates** — "These arcs describe the same work thread"

The `brain_apply.py` script then writes the AI's recommendations back as markers in the vault files.

---

## 7. Adding Markers to Existing Documents

Markers are **additive**. Existing documents work without them. brain_keeper.py's deterministic layer uses frontmatter for heat/promotion; markers add depth.

**When to add markers:**
- New arcs: cluster.py will generate stub files with placeholder marker sections
- Synthesis pass: when kb_brief fills Timeline/Lessons/Resolution, wrap content in `@lesson` blocks
- Manual: when the operator writes a decision, wrap it in `@decision`
- AI-suggested: the reasoning tick may recommend adding markers (future)

**When NOT to add markers:**
- Don't retrofit every line of every document — only structurally important content
- Don't mark obvious content (the title of the document doesn't need a marker)
- Don't use markers as formatting — they're for extraction, not presentation

---

## 8. Anchors

- `stacks/brain-tools/markers.py` — the parser (run `python3 markers.py` for self-test)
- `docs/brain/CLUSTERS.md` — arc schema that markers enhance
- `docs/brain/ARCHITECTURE.md` — full brain system architecture
- `operator/BRAIN-MVP.md` — execution plan tracking
