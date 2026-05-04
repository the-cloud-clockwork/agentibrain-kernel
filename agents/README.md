# agents/

Canonical source of truth for the **brain-keeper** agent definition.

| Agent | Purpose |
|---|---|
| `brain-keeper` | First-class brain-operations agent. Commands: tick / test / triage / enrich / replay / extract / dashboard. Runs on the agenticore image (AGENT_MODE=true). |

## Consumption

Downstream repos clone this directory at install time rather than hosting their
own copy:

```bash
# agentihub install script
git clone --depth=1 https://github.com/The-Cloud-Clockwork/agentibrain-kernel /tmp/abk
cp -r /tmp/abk/agents/brain-keeper/* <agentihub-root>/agents/brain-keeper/
rm -rf /tmp/abk
```

See the top-level README for the full install pattern.

## Files

- `agent.yml` — agenticore manifest (name, model, capabilities, MCP servers).
- `package/CLAUDE.md` — agent instructions.
- `package/system.md` — system prompt.
- `package/.agentihooks.json` — channel subscriptions (brain, amygdala).
