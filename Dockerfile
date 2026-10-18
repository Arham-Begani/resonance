# syntax=docker/dockerfile:1
# One container serving the API (/api) and the built web app (/).
# NOTE: not yet built or tested locally (Docker is unavailable on the development host);
# see README "Deployment" for status.

FROM node:20-bookworm-slim AS web
WORKDIR /app/apps/web
COPY apps/web/package.json apps/web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY apps/web/ ./
# Uses the committed generated API types (src/api/schema.d.ts); no Python needed here.
RUN npm run build

FROM python:3.12-slim-trixie AS app
RUN apt-get update \
    && apt-get install -y --no-install-recommends ngspice \
    && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:0.12 /uv /usr/local/bin/uv
WORKDIR /app/services/api
ENV UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/app/.venv
COPY services/api/pyproject.toml services/api/uv.lock services/api/.python-version ./
RUN uv sync --frozen --no-dev --no-install-project
COPY services/api/ ./
RUN uv sync --frozen --no-dev
COPY --from=web /app/apps/web/dist /app/apps/web/dist
RUN useradd --create-home resonance && mkdir -p /data && chown resonance /data
ENV RESONANCE_DATA_DIR=/data \
    NGSPICE_BIN=ngspice \
    SESSION_COOKIE_SECURE=true \
    PORT=8000
USER resonance
EXPOSE 8000
CMD ["sh", "-c", "/app/.venv/bin/uvicorn resonance.app:app --host 0.0.0.0 --port ${PORT} --proxy-headers"]
