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

# Never run as root: under sudo $HOME resolves to /root, so the vault and
# ~/.agentibrain/.env land in root's home and the stack breaks for the real
# user. Nothing in this script needs elevation. If a previous container run
# left root-owned files in the vault, fix ownership instead of escalating.
if [[ ${EUID:-$(id -u)} -eq 0 && -z "${ALLOW_ROOT_BOOTSTRAP:-}" ]]; then
  echo "ERROR: do not run bootstrap with sudo/root — \$HOME would be /root." >&2
  echo "  Re-run as your normal user:  ./local/bootstrap.sh" >&2
  echo "  Root-owned files in the vault from an old container run? Fix with:" >&2
  echo "    sudo chown -R <you>:<you> <vault-path>   # then re-run without sudo" >&2
  echo "  Set ALLOW_ROOT_BOOTSTRAP=1 only if the stack genuinely runs as root." >&2
  exit 1
fi

# Primary .env lives in the home directory so it survives re-clones.
# The repo-root .env is a symlink to it (auto-created below).
HOME_ENV_DIR="$HOME/.agentibrain"
HOME_ENV_FILE="$HOME_ENV_DIR/.env"
ENV_FILE="$ROOT/.env"
TEMPLATE="$ROOT/local/.env.template"

if [[ ! -f "$TEMPLATE" ]]; then
  echo "ERROR: missing $TEMPLATE" >&2
  exit 1
fi

mkdir -p "$HOME_ENV_DIR"

# 1. Generate / reuse .env in ~/.agentibrain/
if [[ -f "$HOME_ENV_FILE" ]]; then
  echo "[bootstrap] ~/.agentibrain/.env exists — keeping current values"
else
  echo "[bootstrap] generating ~/.agentibrain/.env from template"
  cp "$TEMPLATE" "$HOME_ENV_FILE"
  chmod 600 "$HOME_ENV_FILE"

  if ! command -v openssl >/dev/null 2>&1; then
    echo "ERROR: openssl required to generate tokens" >&2
    exit 1
  fi

  # Replace each __GENERATE__ marker with a fresh random token.
  while grep -q '__GENERATE__' "$HOME_ENV_FILE"; do
    tok="$(openssl rand -hex 32)"
    if sed --version >/dev/null 2>&1; then
      sed -i "0,/__GENERATE__/s//${tok}/" "$HOME_ENV_FILE"   # GNU sed
    else
      sed -i '' "1,/__GENERATE__/s//${tok}/" "$HOME_ENV_FILE" # BSD sed (macOS)
    fi
  done

  echo "[bootstrap] generated random KB_ROUTER_TOKEN + VAULT_READER_TOKENS"
fi

# 1b. Symlink repo-root .env → ~/.agentibrain/.env so docker compose picks it up
#     transparently. Survives re-clones — the source of truth stays in $HOME.
if [[ -L "$ENV_FILE" ]]; then
  echo "[bootstrap] $ENV_FILE symlink already exists"
elif [[ -f "$ENV_FILE" && ! -L "$ENV_FILE" ]]; then
  echo "[bootstrap] $ENV_FILE is a plain file — replacing with symlink to ~/.agentibrain/.env"
  rm "$ENV_FILE"
  ln -s "$HOME_ENV_FILE" "$ENV_FILE"
else
  echo "[bootstrap] creating $ENV_FILE → ~/.agentibrain/.env"
  ln -s "$HOME_ENV_FILE" "$ENV_FILE"
fi

# Alias so the rest of this script keeps working
ENV_FILE="$HOME_ENV_FILE"

# 1c. Ensure EMBEDDINGS_API_KEY (singular, used by consumers) matches
#     EMBEDDINGS_API_KEYS (plural, embeddings service inbound whitelist).
EMB_PLURAL="$(grep -E "^EMBEDDINGS_API_KEYS=" "$ENV_FILE" | head -1 | cut -d= -f2- | tr -d "\r" || true)"
if [[ -n "$EMB_PLURAL" ]]; then
  if grep -qE "^EMBEDDINGS_API_KEY=" "$ENV_FILE"; then
    if sed --version >/dev/null 2>&1; then
      sed -i "s|^EMBEDDINGS_API_KEY=.*|EMBEDDINGS_API_KEY=${EMB_PLURAL}|" "$ENV_FILE"
    else
      sed -i "" "s|^EMBEDDINGS_API_KEY=.*|EMBEDDINGS_API_KEY=${EMB_PLURAL}|" "$ENV_FILE"
    fi
  else
    printf "\n%s=%s\n" "EMBEDDINGS_API_KEY" "$EMB_PLURAL" >> "$ENV_FILE"
  fi
  echo "[bootstrap] aligned EMBEDDINGS_API_KEY = EMBEDDINGS_API_KEYS"
fi

# 2. Resolve vault path (default ~/agentibrain-vault, override via
#    VAULT_ROOT_HOST in .env). The vault lives in $HOME, not the repo checkout,
#    so it survives re-clones and never pollutes the working tree.
DEFAULT_VAULT="$HOME/agentibrain-vault"
VAULT_HOST="$(grep -E '^VAULT_ROOT_HOST=' "$ENV_FILE" | head -1 | cut -d= -f2- | tr -d '\r' || true)"

# The pre-relocation default was ./vault inside the repo — treat it (and empty)
# as "use the current default" so existing .env files upgrade in place.
if [[ -z "$VAULT_HOST" || "$VAULT_HOST" == "./vault" ]]; then
  VAULT_HOST="$DEFAULT_VAULT"
fi

case "$VAULT_HOST" in
  "~") VAULT_ABS="$HOME" ;;
  "~/"*) VAULT_ABS="$HOME/${VAULT_HOST#\~/}" ;;
  /*) VAULT_ABS="$VAULT_HOST" ;;
  *)  VAULT_ABS="$ROOT/${VAULT_HOST#./}" ;;
esac

# Pin the resolved absolute path in .env so docker compose (which reads .env
# directly, without this script) mounts the exact same directory.
if grep -qE '^VAULT_ROOT_HOST=' "$ENV_FILE"; then
  if sed --version >/dev/null 2>&1; then
    sed -i "s|^VAULT_ROOT_HOST=.*|VAULT_ROOT_HOST=${VAULT_ABS}|" "$ENV_FILE"
  else
    sed -i "" "s|^VAULT_ROOT_HOST=.*|VAULT_ROOT_HOST=${VAULT_ABS}|" "$ENV_FILE"
  fi
else
  printf "\nVAULT_ROOT_HOST=%s\n" "$VAULT_ABS" >> "$ENV_FILE"
fi
echo "[bootstrap] vault path: $VAULT_ABS"

# Pin this checkout's location so the agentibrain CLI (build/up/down/logs/
# status) finds the compose deployment from any cwd.
if grep -qE '^AGENTIBRAIN_REPO=' "$ENV_FILE"; then
  if sed --version >/dev/null 2>&1; then
    sed -i "s|^AGENTIBRAIN_REPO=.*|AGENTIBRAIN_REPO=${ROOT}|" "$ENV_FILE"
  else
    sed -i "" "s|^AGENTIBRAIN_REPO=.*|AGENTIBRAIN_REPO=${ROOT}|" "$ENV_FILE"
  fi
else
  printf "\nAGENTIBRAIN_REPO=%s\n" "$ROOT" >> "$ENV_FILE"
fi

# 1b. Pre-create the agentihooks marker-buffer dirs USER-owned. compose binds
#     ~/.agentihooks into tick-cron; if it doesn't exist at first `up`, the
#     Docker daemon (root) auto-creates it root:root 0755 and agentihooks can
#     never write its outbox again.
mkdir -p "$HOME/.agentihooks/brain-outbox"

# 2a. Migrate a legacy in-repo vault. Earlier versions scaffolded ./vault
#     inside the checkout; move its content to the new home if the target
#     doesn't exist yet.
LEGACY_VAULT="$ROOT/vault"
if [[ "$LEGACY_VAULT" != "$VAULT_ABS" && -d "$LEGACY_VAULT" && -n "$(ls -A "$LEGACY_VAULT" 2>/dev/null)" ]]; then
  if [[ ! -d "$VAULT_ABS" ]]; then
    echo "[bootstrap] migrating legacy repo vault $LEGACY_VAULT → $VAULT_ABS"
    mkdir -p "$(dirname "$VAULT_ABS")"
    mv "$LEGACY_VAULT" "$VAULT_ABS"
  else
    echo "[bootstrap] WARNING: legacy vault $LEGACY_VAULT still holds data but $VAULT_ABS already exists — merge it manually, leaving both untouched"
  fi
fi

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

# 2b. Scaffold the full cognitive-region tree from the packaged template.
#     cp -rn is non-clobber, so user edits to existing files are preserved
#     and re-running this script is a no-op.
TEMPLATE_DIR="$ROOT/agentibrain/templates/vault-layout"
if [[ -d "$TEMPLATE_DIR" ]]; then
  cp -rn "$TEMPLATE_DIR"/. "$VAULT_ABS"/
  echo "[bootstrap] vault region tree scaffolded from $TEMPLATE_DIR"
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
