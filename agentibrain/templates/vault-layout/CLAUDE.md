# Vault Rules for AI Agents

## Identity
This vault is the user's external brain. Read `identity/` first for context on any task.

## Structure
- `left/` = technical, systematic, engineering
- `right/` = creative, strategic, life
- `bridge/` = connections between both hemispheres
- `raw/` = unprocessed ingest, never edit directly
- `daily/` = chronological logs, append-only
- `identity/` = who the user is — the root node
- `templates/` = mandatory templates for new notes

## Rules
1. **Never delete files** — archive to `raw/` if obsolete
2. **Every note must have YAML frontmatter:** `date`, `tags`, `status`, `hemisphere`
3. **Use [[wikilinks]]** for all cross-references
4. **Maintain _index.md** in `left/`, `right/`, `bridge/` — regenerate on changes
5. **Decide hemisphere first:** is this technical (`left/`) or creative/strategic (`right/`)?
6. **Daily logs are append-only** — never modify past entries
7. **Cross-hemisphere discoveries** go to `bridge/connections.md`
8. **Check identity/goals.md** before making strategic recommendations
9. **Templates in templates/ are mandatory** for new notes
10. **MUBS instances** are scaffolded from `templates/mubs/` — all placeholders start empty

## MUBS — Minimal Unit of Brain Storage
Every project or idea gets its own directory with the MUBS template:
- VISION.md → Intent, philosophy — the WHY
- SPECS.md → Technical contract — the WHAT
- BLOCKS.md → Major work blocks — the HOW
- TODO.md → Minor items, scratchpad
- STATE.md → Health rating
- BUGS.md, KNOWN-ISSUES.md, ENHANCEMENTS.md, MVP.md, PATCHES.md

A MUBS can start as just a VISION.md — the rest fills in organically.

## Frontmatter Schema
```yaml
---
date: YYYY-MM-DD
tags: [tag1, tag2]
status: draft | active | review | archived
hemisphere: left | right | bridge | identity
type: project | idea | research | reference | decision | incident | daily | strategy
---
```

## Wikilink Conventions
- Link to notes: `[[note-name]]`
- Link to sections: `[[note-name#section]]`
- Link to MUBS: `[[left/projects/<project>/VISION]]`
