# Brain Feed — Hot Arc Feed

The tick-engine writes hot arcs here every tick (default every 2h). Agents and connectors read this folder to get the current state of active cognition.

## Files
- Individual arc markdown files — auto-written, do not edit by hand
- `ticks/` — tick run records (input/output logs)

## Consumers
- agentihooks `brain_adapter` hook — reads this at agent SessionStart to build injected context
- External readers via `brain-adapter` rsync / HTTP

Do not edit content in this folder — it is regenerated every tick. Manual edits will be overwritten.
