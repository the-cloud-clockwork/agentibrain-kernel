#!/usr/bin/env bash
# setup-grafana.sh — Idempotent end-to-end Grafana enablement for the brain.
#
# Bootstraps everything an operator needs to see brain telemetry on a fresh
# (or wiped) Grafana + ClickHouse stack:
#
#   1. Creates `brain` database + `brain.tick_health` table in ClickHouse
#      (matches the schema baked into services/tick-engine/brain_tick.py).
#   2. Installs grafana-clickhouse-datasource plugin if missing.
#   3. Upserts a `clickhouse` datasource in Grafana pointing at the URL.
#   4. Imports observability/brain-health.json as a provisioned dashboard.
#
# Inputs are env vars OR flags. Re-running is safe — every step is idempotent.
#
# Usage:
#   GRAFANA_URL=https://grafana.example.com \
#   GRAFANA_TOKEN=glsa_xxx \
#   CLICKHOUSE_URL=http://user:pass@host:8123 \
#   ./setup-grafana.sh
#
#   # Or with flags:
#   ./setup-grafana.sh \
#     --grafana-url https://grafana.example.com \
#     --grafana-token glsa_xxx \
#     --clickhouse-url http://user:pass@host:8123
#
# Auth options for Grafana (pick one):
#   --grafana-token <service-account-token>     (recommended)
#   --grafana-user <user> --grafana-password <pw>
#
# Optional:
#   --datasource-uid clickhouse        (default: clickhouse)
#   --folder-uid agentibrain           (default: agentibrain)
#   --folder-title "Brain"             (default: "Brain")
#   --skip-plugin-install              (skip Grafana plugin install step)
#   --skip-schema                      (skip ClickHouse schema bootstrap)

set -euo pipefail

GRAFANA_URL="${GRAFANA_URL:-}"
GRAFANA_TOKEN="${GRAFANA_TOKEN:-}"
GRAFANA_USER="${GRAFANA_USER:-}"
GRAFANA_PASSWORD="${GRAFANA_PASSWORD:-}"
CLICKHOUSE_URL="${CLICKHOUSE_URL:-}"
DATASOURCE_UID="${DATASOURCE_UID:-clickhouse}"
FOLDER_UID="${FOLDER_UID:-agentibrain}"
FOLDER_TITLE="${FOLDER_TITLE:-Brain}"
SKIP_PLUGIN_INSTALL="${SKIP_PLUGIN_INSTALL:-0}"
SKIP_SCHEMA="${SKIP_SCHEMA:-0}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DASHBOARD_FILE="${DASHBOARD_FILE:-${SCRIPT_DIR}/brain-health.json}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --grafana-url)        GRAFANA_URL="$2"; shift 2 ;;
    --grafana-token)      GRAFANA_TOKEN="$2"; shift 2 ;;
    --grafana-user)       GRAFANA_USER="$2"; shift 2 ;;
    --grafana-password)   GRAFANA_PASSWORD="$2"; shift 2 ;;
    --clickhouse-url)     CLICKHOUSE_URL="$2"; shift 2 ;;
    --datasource-uid)     DATASOURCE_UID="$2"; shift 2 ;;
    --folder-uid)         FOLDER_UID="$2"; shift 2 ;;
    --folder-title)       FOLDER_TITLE="$2"; shift 2 ;;
    --dashboard-file)     DASHBOARD_FILE="$2"; shift 2 ;;
    --skip-plugin-install) SKIP_PLUGIN_INSTALL=1; shift ;;
    --skip-schema)        SKIP_SCHEMA=1; shift ;;
    -h|--help)            sed -n '2,40p' "$0"; exit 0 ;;
    *) echo "unknown flag: $1" >&2; exit 2 ;;
  esac
done

die() { echo "ERROR: $*" >&2; exit 1; }
log() { printf '\033[0;34m[brain-setup]\033[0m %s\n' "$*"; }
ok()  { printf '\033[0;32m[brain-setup ✓]\033[0m %s\n' "$*"; }

[[ -n "$GRAFANA_URL" ]]    || die "GRAFANA_URL is required"
[[ -n "$CLICKHOUSE_URL" ]] || die "CLICKHOUSE_URL is required"
[[ -f "$DASHBOARD_FILE" ]] || die "dashboard file not found: $DASHBOARD_FILE"

GRAFANA_URL="${GRAFANA_URL%/}"

if [[ -n "$GRAFANA_TOKEN" ]]; then
  GRAFANA_AUTH=(-H "Authorization: Bearer ${GRAFANA_TOKEN}")
elif [[ -n "$GRAFANA_USER" && -n "$GRAFANA_PASSWORD" ]]; then
  GRAFANA_AUTH=(-u "${GRAFANA_USER}:${GRAFANA_PASSWORD}")
else
  die "provide --grafana-token OR --grafana-user + --grafana-password"
fi

command -v jq    >/dev/null || die "jq is required"
command -v curl  >/dev/null || die "curl is required"

# --- 1. ClickHouse schema bootstrap ------------------------------------------
if [[ "$SKIP_SCHEMA" != "1" ]]; then
  log "Bootstrapping brain schema in ClickHouse..."
  curl -sS -f -X POST "${CLICKHOUSE_URL}" --data \
    "CREATE DATABASE IF NOT EXISTS brain" >/dev/null \
    || die "failed to create brain database (check CLICKHOUSE_URL credentials)"

  curl -sS -f -X POST "${CLICKHOUSE_URL}" --data "
    CREATE TABLE IF NOT EXISTS brain.tick_health (
      timestamp DateTime DEFAULT now(),
      score Float32,
      reason String,
      arcs_scanned UInt32,
      signals_collected UInt32,
      lessons_collected UInt32,
      heat_changes UInt32,
      promotions UInt32,
      demotions UInt32,
      graduations UInt32,
      hot_arcs_written UInt32,
      total_ms UInt32,
      tick_type String,
      signals_written UInt32,
      signals_tombstoned_stale UInt32,
      signals_tombstoned_cleared UInt32
    ) ENGINE = MergeTree
      ORDER BY timestamp
      TTL timestamp + INTERVAL 90 DAY
  " >/dev/null || die "failed to create brain.tick_health table"
  ok "brain.tick_health ready (90-day TTL)"
fi

# --- 2. Plugin install -------------------------------------------------------
if [[ "$SKIP_PLUGIN_INSTALL" != "1" ]]; then
  log "Checking grafana-clickhouse-datasource plugin..."
  plugin_status=$(curl -sS "${GRAFANA_AUTH[@]}" \
    "${GRAFANA_URL}/api/plugins/grafana-clickhouse-datasource/settings" \
    -o /dev/null -w '%{http_code}' || true)
  if [[ "$plugin_status" == "200" ]]; then
    ok "plugin already installed"
  else
    log "Installing plugin via Grafana admin API..."
    install_resp=$(curl -sS "${GRAFANA_AUTH[@]}" \
      -X POST -H "Content-Type: application/json" \
      "${GRAFANA_URL}/api/plugins/grafana-clickhouse-datasource/install" \
      -d '{}' || true)
    if echo "$install_resp" | grep -qi "installed\|409\|already"; then
      ok "plugin install accepted"
    else
      log "WARN: install API returned: $install_resp"
      log "Fallback: install on the Grafana host with:"
      log "  GF_INSTALL_PLUGINS=grafana-clickhouse-datasource (env var, restart)"
    fi
  fi
fi

# --- 3. Datasource upsert ----------------------------------------------------
log "Upserting ClickHouse datasource (uid=${DATASOURCE_UID})..."
ds_payload=$(jq -n \
  --arg uid    "$DATASOURCE_UID" \
  --arg name   "ClickHouse" \
  --arg url    "$(echo "$CLICKHOUSE_URL" | sed -E 's|(://)[^@]+@|\1|')" \
  --arg user   "$(echo "$CLICKHOUSE_URL" | sed -nE 's|.*://([^:]+):.*@.*|\1|p')" \
  --arg pass   "$(echo "$CLICKHOUSE_URL" | sed -nE 's|.*://[^:]+:([^@]+)@.*|\1|p')" \
  '{
    uid: $uid, name: $name,
    type: "grafana-clickhouse-datasource",
    access: "proxy", url: $url, isDefault: false,
    jsonData: { defaultDatabase: "brain", protocol: "http", port: 8123, server: $url },
    secureJsonData: ($pass | if . == "" then {} else { password: $pass } end),
    user: $user
  }')

# Try update; fall back to create. Respect file-provisioned datasources.
existing_body=$(curl -sS "${GRAFANA_AUTH[@]}" \
  "${GRAFANA_URL}/api/datasources/uid/${DATASOURCE_UID}" || true)
existing_uid=$(echo "$existing_body" | jq -r '.uid // empty' 2>/dev/null || true)

if [[ -n "$existing_uid" ]]; then
  read_only=$(echo "$existing_body" | jq -r '.readOnly // false')
  ds_type=$(echo "$existing_body" | jq -r '.type // ""')
  if [[ "$read_only" == "true" ]]; then
    if [[ "$ds_type" == "grafana-clickhouse-datasource" ]]; then
      ok "datasource already provisioned (file-based, read-only) — skipping update"
    else
      die "uid '${DATASOURCE_UID}' is provisioned with wrong type: ${ds_type}"
    fi
  else
    ds_id=$(echo "$existing_body" | jq -r '.id')
    curl -sS -f "${GRAFANA_AUTH[@]}" -X PUT \
      -H "Content-Type: application/json" \
      "${GRAFANA_URL}/api/datasources/${ds_id}" \
      -d "$ds_payload" >/dev/null || die "datasource update failed"
    ok "datasource updated"
  fi
else
  curl -sS -f "${GRAFANA_AUTH[@]}" -X POST \
    -H "Content-Type: application/json" \
    "${GRAFANA_URL}/api/datasources" \
    -d "$ds_payload" >/dev/null || die "datasource create failed"
  ok "datasource created"
fi

# --- 4. Folder + Dashboard import --------------------------------------------
log "Ensuring folder ${FOLDER_TITLE} (uid=${FOLDER_UID})..."
folder_status=$(curl -sS "${GRAFANA_AUTH[@]}" \
  "${GRAFANA_URL}/api/folders/${FOLDER_UID}" \
  -o /dev/null -w '%{http_code}' || true)
if [[ "$folder_status" != "200" ]]; then
  curl -sS -f "${GRAFANA_AUTH[@]}" -X POST \
    -H "Content-Type: application/json" \
    "${GRAFANA_URL}/api/folders" \
    -d "$(jq -n --arg uid "$FOLDER_UID" --arg t "$FOLDER_TITLE" '{uid:$uid,title:$t}')" \
    >/dev/null || die "folder create failed"
  ok "folder created"
else
  ok "folder exists"
fi

log "Importing dashboard ${DASHBOARD_FILE}..."
import_payload=$(jq -n \
  --slurpfile d "$DASHBOARD_FILE" \
  --arg folderUid "$FOLDER_UID" \
  '{ dashboard: ($d[0] | .id = null), folderUid: $folderUid, overwrite: true, message: "agentibrain bootstrap" }')

import_resp=$(curl -sS "${GRAFANA_AUTH[@]}" -X POST \
  -H "Content-Type: application/json" \
  "${GRAFANA_URL}/api/dashboards/db" \
  -d "$import_payload")

if echo "$import_resp" | jq -e '.status == "success"' >/dev/null 2>&1; then
  url=$(echo "$import_resp" | jq -r '.url')
  ok "dashboard imported: ${GRAFANA_URL}${url}"
else
  die "dashboard import failed: $import_resp"
fi

ok "brain Grafana setup complete"
