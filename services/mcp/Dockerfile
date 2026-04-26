FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends curl tini && \
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && \
    apt-get install -y --no-install-recommends nodejs && \
    npm install -g mcp-proxy@6.4.0 && \
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
HEALTHCHECK --interval=30s --timeout=5s CMD curl -sf http://127.0.0.1:8080/ping || exit 1
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/entrypoint.sh"]
