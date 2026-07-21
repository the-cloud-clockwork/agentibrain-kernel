# Broadcasts

## Reading

`BROADCAST` blocks arrive in your context from two channels: `brain` (hot arcs,
signals, operator intent, what changed last pass) and `amygdala` (emergencies
only). Read them — they are what the fleet knows right now, not preamble.

If you see no `BROADCAST` blocks at all, your session is not subscribed.

## Writing

```
channel_publish(channel="brain", message="restarting litellm-0, hold off on MCP calls", severity="info")
```

Coordination only, and rarely — every broadcast spends attention across every
agent. Knowledge goes to markers and `brain_ingest`; milestones and signals
already broadcast themselves.
