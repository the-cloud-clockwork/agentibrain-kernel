#!/usr/bin/env bash
set -euo pipefail

# agentibrain-kernel — local bootstrap.
# Idempotent. Safe to re-run.
#
# What it does:
#   1. Generates .env from local/.env.template (root-level), filling in
#      __GENERATE__ markers with random 32-byte hex tokens.
#   2. Scaffolds the vault directory tree (brain-feed, clusters, raw/inbox).
#   3. Prints next-step instructions.

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

ENV_FILE="$ROOT/.env"
TEMPLATE="$ROOT/local/.env.template"

if [[ ! -f "$TEMPLATE" ]]; then
  echo "ERROR: missing $TEMPLATE" >&2
  exit 1
fi

# 1. Generate / reuse .env
if [[ -f "$ENV_FILE" ]]; then
  echo "[bootstrap] .env exists — keeping current values"
else
  echo "[bootstrap] generating .env from template"
  cp "$TEMPLATE" "$ENV_FILE"

  if ! command -v openssl >/dev/null 2>&1; then
    echo "ERROR: openssl required to generate tokens" >&2
    exit 1
  fi

  # Replace each __GENERATE__ marker with a fresh random token.
  while grep -q '__GENERATE__' "$ENV_FILE"; do
    tok="$(openssl rand -hex 32)"
    # sed -i differs between macOS and Linux. Use a portable form.
    if sed --version >/dev/null 2>&1; then
      sed -i "0,/__GENERATE__/s//${tok}/" "$ENV_FILE"   # GNU sed
    else
      sed -i '' "1,/__GENERATE__/s//${tok}/" "$ENV_FILE" # BSD sed (macOS)
    fi
  done

  echo "[bootstrap] generated random KB_ROUTER_TOKEN + VAULT_READER_TOKENS"
fi

# 2. Resolve vault path (default ./vault, override via VAULT_ROOT_HOST in .env)
VAULT_HOST="$(grep -E '^VAULT_ROOT_HOST=' "$ENV_FILE" | head -1 | cut -d= -f2- | tr -d '\r' || echo './vault')"
[[ -z "$VAULT_HOST" ]] && VAULT_HOST="./vault"

# Resolve relative to repo root
case "$VAULT_HOST" in
  /*) VAULT_ABS="$VAULT_HOST" ;;
  *)  VAULT_ABS="$ROOT/${VAULT_HOST#./}" ;;
esac

if [[ ! -d "$VAULT_ABS" ]]; then
  echo "[bootstrap] scaffolding vault at $VAULT_ABS"
  mkdir -p "$VAULT_ABS"/{brain-feed,clusters,raw/inbox}
  cat > "$VAULT_ABS/README.md" <<'EOF'
# Local agentibrain vault

This directory is the brain's working memory:

- `raw/inbox/` — incoming markers (one .md per marker)
- `brain-feed/` — generated injection blocks (`hot-arcs.md`, `signals.md`, `last-tick.md`, …)
- `clusters/` — arc cluster files (one .md per active arc)

To use your real Obsidian vault instead, set `VAULT_ROOT_HOST=/path/to/your/vault`
in `.env` and re-run `docker compose up`.
EOF
else
  echo "[bootstrap] vault $VAULT_ABS already present — leaving alone"
  mkdir -p "$VAULT_ABS"/{brain-feed,clusters,raw/inbox}
fi

# 3. Done.
echo
echo "=== agentibrain-kernel local bootstrap ready ==="
echo "Next:  docker compose up -d"
echo "       docker compose ps"
echo "       curl -H \"Authorization: Bearer \$(grep ^KB_ROUTER_TOKEN .env | cut -d= -f2)\" http://localhost:8103/feed | jq ."
echo
echo "For local AI tick (Ollama overlay):"
echo "       docker compose -f compose.yml -f local/compose.ollama.yml up -d"
echo "       docker compose exec ollama ollama pull llama3.2"
echo
