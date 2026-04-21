# Templates

Starting points for new notes. Copy, rename, fill in.

## Single-note templates
| Template | Use when creating |
|----------|-------------------|
| `decision.md` | An architectural decision record (ADR) — one file, lives in `left/decisions/` |
| `idea.md` | A quick-capture idea — `right/ideas/` |
| `incident.md` | An incident post-mortem — `left/incidents/` or `amygdala/` while active |
| `project.md` | A lightweight pointer to a project — use MUBS instead for anything real |
| `research.md` | A research spike — `left/research/` |

## MUBS templates (`mubs/`)
MUBS = Minimal Unit of Brain Storage. Every non-trivial project or idea gets its own directory with the MUBS template files. Copy the whole `mubs/` folder:

```bash
cp -r templates/mubs/* left/projects/<your-project>/
```

Files in a MUBS:
- **VISION.md** — Intent, philosophy, the WHY
- **SPECS.md** — Technical contract, the WHAT
- **BLOCKS.md** — Major work blocks, the HOW
- **TODO.md** — Minor items, scratchpad
- **STATE.md** — Current health rating
- **BUGS.md**, **KNOWN-ISSUES.md**, **ENHANCEMENTS.md**, **MVP.md**, **PATCHES.md** — fill in as the project grows

A MUBS can start as just a `VISION.md` — the rest fills in organically.
