# Multi-stage isn't needed; small base image is enough.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# system deps (none strictly required; curl_cffi bundles its own TLS backend)
RUN apt-get update -qq && apt-get install -y --no-install-recommends \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app.py db.py firefly_pipeline.py models_catalog.py wsgi.py ./

# data dir (rendered as volume on Fly.io / Render; ephemeral on plain free)
RUN mkdir -p /data

EXPOSE 8080

# Entrypoint writes the Adobe credentials from env vars (set via `fly secrets set`)
# into /data so app.py and firefly_pipeline.py find them at $FIREFLY_DATA_DIR.
# Then exec gunicorn.
CMD ["sh", "-c", "\
  set -e; \
  if [ -n \"$STORAGE_JSON\" ]; then printf '%s' \"$STORAGE_JSON\" > /data/storage.json; fi; \
  if [ -n \"$TOKEN_JSON\" ]; then printf '%s' \"$TOKEN_JSON\" > /data/current_token.json; fi; \
  exec gunicorn -w 2 -k gthread --threads 4 --timeout 600 --bind 0.0.0.0:8080 wsgi:app"]