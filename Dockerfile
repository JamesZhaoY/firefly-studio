# Multi-stage build:
#   stage 1: build the React frontend (Node)
#   stage 2: Python + gunicorn + Nginx serving frontend + /api/*

# ── Stage 1: build frontend ─────────────────────────────────
FROM node:20-alpine AS frontend

WORKDIR /web

COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci

COPY frontend/ ./
# Set base path so assets load from /firefly-studio/...
# (matches the GitHub Pages path; harmless if served at root)
ARG VITE_PAGES_BASE=/firefly-studio/
ARG VITE_API_BASE=
ENV VITE_PAGES_BASE=${VITE_PAGES_BASE}
ENV VITE_API_BASE=${VITE_API_BASE}
RUN npm run build

# ── Stage 2: runtime (python + nginx) ──────────────────────
FROM python:3.11-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# nginx + ca-certs + tzdata. nginx is small enough to keep here.
RUN apt-get update -qq && apt-get install -y --no-install-recommends \
        ca-certificates tzdata nginx \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python deps (cached layer)
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

# App code
COPY app.py db.py firefly_pipeline.py models_catalog.py wsgi.py ./

# Frontend build artifacts → nginx serves these
COPY --from=frontend /web/dist /web/frontend/dist

# Persistent data + outputs
RUN mkdir -p /data /app/outputs

# nginx config: serve static + reverse-proxy /api/* to gunicorn
COPY nginx.conf /etc/nginx/nginx.conf

EXPOSE 19999

# Healthcheck hits the API through nginx (mirrors what users see)
HEALTHCHECK --interval=30s --timeout=5s --retries=5 --start-period=20s \
    CMD wget --quiet --spider http://127.0.0.1:19999/api/health || exit 1

# Single entrypoint: nginx (background) + gunicorn (foreground)
CMD ["sh", "-c", "\
  set -e; \
  mkdir -p /data; \
  if [ -n \"$STORAGE_JSON\" ]; then printf '%s' \"$STORAGE_JSON\" > /data/storage.json; fi; \
  if [ -n \"$TOKEN_JSON\" ]; then printf '%s' \"$TOKEN_JSON\" > /data/current_token.json; fi; \
  nginx; \
  exec gunicorn -w 2 -k gthread --threads 4 --timeout 600 \
    --bind 127.0.0.1:19998 wsgi:app"]