# Your agentibrain vault

This directory is your external brain. It was scaffolded by `brain scaffold` and is owned by you — edit anything except `.brain-schema`.

Open it in [Obsidian](https://obsidian.md) (or any markdown editor) to browse it visually — wikilinks, backlinks, and graph view all work out of the box. Nothing in this tree is Obsidian-specific; the folder layout is the contract.

## Quick tour

| Region | What lives here |
|---|---|
| `identity/` | Who you are — about-me, goals, principles, stack. **Fill these first.** |
| `left/` | Technical + systematic — projects, research, reference, decisions (ADRs), incidents |
| `right/` | Creative + strategic — ideas, strategy, life, risk, creative |
| `bridge/` | Cross-hemisphere connections — the synthesis layer |
| `clusters/` | Canonical arc storage — written by the tick-engine |
| `frontal-lobe/` | Hot working memory — auto-injected arcs, demotes to hemispheres over time |
| `amygdala/` | Emergency signals — broadcast to every agent |
| `pineal/` | Wins + breakthroughs — the memory the system returns to for "what was good this week" |
| `brain-feed/` | Tick output feed — read by connectors |
| `raw/` | Ingest staging — inbox, articles, media, transcripts |
| `daily/` | Append-only daily logs |
| `templates/` | Note + MUBS templates for new content |

## First steps

1. Open `CLAUDE.md` — it defines the vault rules for AI agents.
2. Fill `identity/about-me.template.md` → rename to `about-me.md`. Same for `goals`, `principles`, `stack`.
3. Create your first project as a MUBS — copy `templates/mubs/` to `left/projects/<your-project>/`.
4. Let the tick-engine run — it writes to `clusters/` and `brain-feed/` for you.

## Don't touch

- `.brain-schema` — schema version marker, managed by `brain scaffold`. Do not edit.
