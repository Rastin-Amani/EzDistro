# Use a lightweight Python image
FROM python:3.12-slim

WORKDIR /code

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY docs ./docs
COPY docker-entrypoint.sh .

ENV PYTHONUNBUFFERED=1

# Web + a pool of worker processes in one container (Dokploy "Dockerfile" deploy).
# Scale the pool with the WORKERS env var (default 4) — no rebuild, no replica.
CMD ["sh", "docker-entrypoint.sh"]

# Worker only (override CMD): docker run --rm ezdistro python -m app.workers.worker
