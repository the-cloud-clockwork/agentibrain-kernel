# observability/

Grafana enablement assets for the brain.

| File | Purpose |
|---|---|
| `brain-health.json` | 27-panel Grafana dashboard, six brain regions. |
| `setup-grafana.sh` | Idempotent bootstrap: ClickHouse schema → datasource → dashboard. |

## Quick start

```bash
GRAFANA_URL=https://grafana.example.com \
GRAFANA_TOKEN=glsa_xxx \
CLICKHOUSE_URL=http://default:secret@clickhouse-host:8123 \
./setup-grafana.sh
```

Or via flags:

```bash
./setup-grafana.sh \
  --grafana-url https://grafana.example.com \
  --grafana-token glsa_xxx \
  --clickhouse-url http://default:secret@clickhouse-host:8123
```

The script:

1. Creates `brain` database + `brain.tick_health` table in ClickHouse
   (matches the schema `services/tick-engine/brain_tick.py` writes).
2. Installs `grafana-clickhouse-datasource` plugin (Grafana 10+ admin API).
3. Upserts a `clickhouse` datasource pointing at the ClickHouse URL.
4. Creates a `Brain` folder and imports `brain-health.json`.

Re-running is safe — every step is idempotent.

## Auth

Pick one:

- `--grafana-token <service-account-token>` (recommended, rotatable)
- `--grafana-user <user> --grafana-password <pw>`

## Plugin install fallback

If `--skip-plugin-install` is set or the admin API rejects (some Grafana
distros disable runtime plugin install), set this on the Grafana host
instead:

```yaml
environment:
  - GF_INSTALL_PLUGINS=grafana-clickhouse-datasource
```

…then restart Grafana once. The plugin auto-installs on boot from then on.

## What it does NOT do

- It does not provision the `tick-engine` cron — that's owned by
  `helm/brain-cron/`.
- It does not seed historical data — the dashboard fills as soon as the
  next tick lands a row.
