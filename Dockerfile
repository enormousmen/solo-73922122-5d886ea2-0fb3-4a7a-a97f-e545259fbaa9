# syntax=docker/dockerfile:1
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# The service needs no privileges; run as a system user.
RUN useradd --system --no-create-home --shell /usr/sbin/nologin appuser

# Staging store: the seed ships in the image; the live snapshot lives on a
# writable volume so applied tickets survive restarts (root fs is read-only).
# /data is owned by appuser so a fresh named volume inherits a writable dir.
ENV STAGING_SEED_PATH=/app/seed/staging-seed.json \
    STAGING_SNAPSHOT_PATH=/data/staging-snapshot.json
RUN mkdir -p /data && chown appuser:appuser /data
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2).status == 200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
