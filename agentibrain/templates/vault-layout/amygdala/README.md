# Amygdala — Emergency Broadcast Region

Active incidents mid-resolution live here. Every file in this folder is a signal that can fire a fleet-wide alarm through the agentihooks broadcast layer.

## Semantics
- **Entry**: an arc lands here when signals cross a severity threshold (nuclear / critical / warning)
- **Broadcast**: the brain-amygdala daemon reads this folder on cron and publishes `brain_amygdala_alarm` to every running agent via the event bus
- **Exit**: after 15 min of signal-clear, auto-downgrade; after 7 days resolved, graduate to `left/incidents/`

## Severity
- **nuclear** — halts all non-critical work across the fleet
- **critical** — agents pause on matching intent; user notified immediately
- **warning** — logged, escalation threshold
