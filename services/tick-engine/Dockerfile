FROM python:3.12-slim

WORKDIR /app
COPY *.py ./

RUN pip install --no-cache-dir redis

ENTRYPOINT ["python3"]
