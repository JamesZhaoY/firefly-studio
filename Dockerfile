# Lightweight Python image for the Firefly Studio backend
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# curl_cffi bundles its own TLS backend; just need ca-certs + tzdata.
RUN apt-get update -qq && apt-get install -y --no-install-recommends \
        ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first (cached layer)
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

# App code
COPY app.py db.py firefly_pipeline.py models_catalog.py wsgi.py ./

# Persistent data + outputs dirs
RUN mkdir -p /data /app/outputs

# Expose one port for the API
EXPOSE 19999

# Entrypoint: write secret-injected credentials to /data, then exec gunicorn.
# Gunicorn binds to 19999 (overridable via PORT env).
CMD ["sh", "-c", "\
  set -e; \
  mkdir -p /data; \
  if [ -n \"$STORAGE_JSON\" ]; then printf '%s' \"$STORAGE_JSON\" > /data/storage.json; fi; \
  if [ -n \"$TOKEN_JSON\" ]; then printf '%s' \"$TOKEN_JSON\" > /data/current_token.json; fi; \
  exec gunicorn -w 2 -k gthread --threads 4 --timeout 600 \
    --bind 0.0.0.0:${PORT:-19999} wsgi:app"]