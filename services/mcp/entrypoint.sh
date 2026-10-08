#!/bin/sh
set -eu

# mcp-proxy reads every MCP_PROXY_* variable itself, MCP_PROXY_API_KEY
# included, so the key never appears on a command line.
MCP_AUTH_MODE="${MCP_AUTH_MODE-required}"
case "${MCP_AUTH_MODE}" in
    required)
        if [ -z "${MCP_PROXY_API_KEY:-}" ]; then
            echo "MCP_AUTH_MODE=required: MCP_PROXY_API_KEY is absent or empty, refusing to start" >&2
            exit 64
        fi
        ;;
    local) ;;
    *)
        echo "MCP_AUTH_MODE=${MCP_AUTH_MODE} is not required or local, refusing to start" >&2
        exit 64
        ;;
esac

export MCP_PROXY_PORT="${MCP_PROXY_PORT:-8080}"
if [ "${MCP_PROXY_STATELESS:-true}" = "true" ]; then
    exec mcp-proxy --stateless -- python3 /app/server/server.py
fi
exec mcp-proxy -- python3 /app/server/server.py
