# Broadcast Protocol — Fleet Communication (Priority 3)

## Reading Broadcasts

You receive broadcasts as `BROADCAST` blocks in your context injection. Sources:
- **brain-adapter** — hot arcs, signals, inject blocks, operator intent, tick diffs
- **amygdala** — emergency signals (nuclear/critical severity)

Always read broadcast content. It tells you what the fleet knows right now.

## Writing Broadcasts

Use `channel_publish` (hooks-utils MCP) to send messages to the fleet:

```
channel_publish(
  channel="brain",       # or "ops-alerts", "deploy-status"
  message="I'm about to restart litellm-0, hold off on MCP calls",
  severity="info"        # "info", "alert", "critical"
)
```

- Use sparingly — every broadcast costs attention across all agents
- Milestones and signals auto-broadcast via markers (preferred path)
- Direct broadcast is for coordination: "I'm about to restart X, hold off on Y"

## Channel Subscriptions

Your session receives broadcasts from channels listed in `.agentihooks.json` at your CWD:

```json
{"channels": ["brain", "amygdala"]}
```

Without this file, brain/amygdala broadcasts are published but filtered out at delivery. If you don't see `BROADCAST` blocks in your context injection, the subscription is missing.
