FROM python:3.12-slim

WORKDIR /app
COPY *.py ./
COPY scripts/ ./scripts/

RUN pip install --no-cache-dir redis

ENTRYPOINT ["python3"]
