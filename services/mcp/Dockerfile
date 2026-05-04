# Stage 1 — install mcp-proxy via npm using Node's official slim image.
# This avoids piping a third-party install script to bash (the previous
# `curl https://deb.nodesource.com/setup_20.x | bash` approach), which fails
# on corporate networks with TLS inspection and is also a supply-chain risk.
FROM node:20-slim AS node-stage
RUN npm install -g mcp-proxy@6.4.0

# Stage 2 — Python runtime + Node binaries copied from stage 1.
FROM python:3.12-slim

# Bring in Node and the mcp-proxy global install from the node stage.
# `mcp-proxy` is an ESM script that imports siblings relative to its file
# location, so the binary must live INSIDE the node_modules tree — copying
# the symlink target directly would break import paths. Instead, copy the
# whole node_modules tree and recreate the symlink in /usr/local/bin/.
COPY --from=node-stage /usr/local/bin/node /usr/local/bin/node
COPY --from=node-stage /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/mcp-proxy/dist/bin/mcp-proxy.mjs /usr/local/bin/mcp-proxy

RUN apt-get update && apt-get install -y --no-install-recommends curl tini && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ /app/server/

COPY <<'ENTRYPOINT' /usr/local/bin/entrypoint.sh
#!/bin/sh
set -e
MCP_PROXY_PORT="${MCP_PROXY_PORT:-8080}"
MCP_PROXY_STATELESS="${MCP_PROXY_STATELESS:-true}"
CMD="mcp-proxy --port ${MCP_PROXY_PORT}"
if [ "${MCP_PROXY_STATELESS}" = "true" ]; then
    CMD="${CMD} --stateless"
fi
if [ -n "${MCP_PROXY_API_KEY}" ]; then
    CMD="${CMD} --apiKey ${MCP_PROXY_API_KEY}"
fi
exec ${CMD} -- python3 /app/server/server.py
ENTRYPOINT
RUN chmod +x /usr/local/bin/entrypoint.sh

EXPOSE 8080
# /ping is served by mcp-proxy itself (verified: returns "pong" + HTTP 200,
# no auth required). The Python FastMCP server runs as a stdio subprocess
# of mcp-proxy and does not need its own /ping route.
HEALTHCHECK --interval=30s --timeout=5s CMD curl -sf http://127.0.0.1:8080/ping || exit 1
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/entrypoint.sh"]
