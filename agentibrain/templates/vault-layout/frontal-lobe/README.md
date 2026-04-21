# Frontal Lobe — Conscious Working Memory

Hot arcs currently in active conversation. Everything here is either being worked on RIGHT NOW or was worked on in the last 24-72h and still warm.

## Subdirectories
- **conscious/** — arcs with heat ≥ 7, auto-injected into agent CLAUDE.md
- **unconscious/** — cooled arcs still linked by edges but no longer auto-injected

## Promotion / demotion
- brain-keeper daemon recomputes heat on cron (30 min)
- heat ≥ 7 → conscious/
- heat drops < 5 → unconscious/
- heat drops < 2 → graduate to left/ or right/ by region
