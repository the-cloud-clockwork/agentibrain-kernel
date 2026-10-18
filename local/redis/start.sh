#!/usr/bin/env bash
set -euo pipefail

acl_file="${1:-/data/users.acl}"
umask 077
printf '%s\n' 'user default on nopass ~* &* +@all -flushall -flushdb' > "$acl_file"
if [[ -n "${SWARM_REDIS_PASSWORD:-}" ]]; then
  hash="$(printf '%s' "$SWARM_REDIS_PASSWORD" | sha256sum | cut -d' ' -f1)"
  printf 'user swarm on #%s ~* &* +@all -flushall -flushdb\n' "$hash" >> "$acl_file"
fi
chmod 600 "$acl_file"
exec docker-entrypoint.sh redis-server --appendonly yes --aclfile "$acl_file"
