# Broadcast Protocol — Fleet Communication

## Reading Broadcasts

You receive broadcasts as `BROADCAST` blocks in your context injection. These come from:
- **brain-adapter** — hot arcs, signals, inject blocks, operator intent
- **amygdala** — emergency signals (nuclear/critical)
- **other agents** — milestone announcements, status updates (when broadcast MCP is available)

Always read broadcast content. It tells you what the fleet knows right now.

## Writing Broadcasts (Future — MCP Tool)

When the broadcast MCP tool is available, you can publish messages to the fleet:
- Use sparingly — every broadcast costs attention across all agents
- Milestones and signals are automatically broadcast via markers (preferred path)
- Direct broadcast is for coordination: "I'm about to restart X, hold off on Y"

## Channel Subscriptions

Your project's `.agentihooks.json` defines which channels you receive:
- `brain` — hot arcs, inject blocks, lessons, operator intent
- `amygdala` — emergency signals only

If you don't see brain context in your broadcasts, the project is missing `"channels": ["brain", "amygdala"]` in `.agentihooks.json`.
