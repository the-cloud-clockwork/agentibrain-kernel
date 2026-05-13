FROM python:3.12-slim

WORKDIR /app

# git for repo clones, tini for signal handling, curl for healthcheck
RUN apt-get update && apt-get install -y --no-install-recommends git curl tini \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ /app/app/

ENV PYTHONUNBUFFERED=1
# Force httpx/certifi to use the system CA bundle (contains up-to-date Let's Encrypt + ISRG roots)
ENV SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt
ENV REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s CMD curl -sf http://127.0.0.1:8080/health || exit 1
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
