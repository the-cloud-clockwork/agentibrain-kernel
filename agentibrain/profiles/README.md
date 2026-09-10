# profiles/

Agentihooks profile overlays owned by the brain.

| Profile | Purpose |
|---|---|
| `brain` | Generic brain overlay — adds marker emission rules (@lesson / @milestone / @signal / @decision) and broadcast protocol. Chainable on top of any base profile. |
| `brain-keeper` | Full behavioral profile for the brain-keeper agent itself. |

## Consumption

`agentihooks-bundle` clones these at install time instead of hosting its own
copies. See the top-level README for the full install pattern.

## What lives here

- Markers, tools and channel discipline all live in `brain/CLAUDE.md`; the profile ships no separate rules.
- `brain/profile.yml` — overlay metadata + activation conditions.
- `brain-keeper/CLAUDE.md` — keeper agent behavioral guide.
- `brain-keeper/profile.yml` — keeper profile metadata.
