# Emporia relay — single container: FastAPI relay + embedded SRCL dashboard.
#
# Serves everything on one port (default 8088): REST/WS API + dashboard at /ui/.
# There is no separate Vite dev port (5173) in this image — that's a local-dev-only
# server, not used for SSH/VPS/server deployment.
#
#   docker build -t emporia-relay .
#   docker run -p 8088:8088 -v emporia-data:/data --env-file .env emporia-relay
#
# See docker-compose.yml for a ready-to-run stack (env file, volume, optional profiles).

# ── Stage 1: build the embedded dashboard (Vite + React + SRCL) ─────────────
FROM node:20-slim AS dashboard-builder
WORKDIR /build/dashboard
COPY dashboard/package.json dashboard/package-lock.json* ./
RUN npm ci
COPY dashboard/ ./
# VITE_RELAY_URL='' → relative URLs; dashboard is served from the same origin as the API.
RUN npm run build:embedded

# ── Stage 2: relay runtime ───────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

RUN pip install --no-cache-dir uv

WORKDIR /app

# Install Python deps first (better layer caching on code-only changes)
COPY pyproject.toml uv.lock ./
COPY src/ ./src/
RUN uv sync --frozen --no-dev

# Embedded dashboard build output — relay serves this at /ui/ (StaticFiles on dashboard/dist)
COPY --from=dashboard-builder /build/dashboard/dist ./dashboard/dist

# Ed25519 keys, SQLite DB, and JSONL audit logs must survive container restarts.
ENV EMPORIA_KEYS_DIR=/data/keys \
    EMPORIA_DB_PATH=/data/emporia.sqlite3 \
    EMPORIA_LOG_DIR=/data/logs \
    EMPORIA_RELAY_PORT=8088 \
    PATH="/app/.venv/bin:${PATH}"
VOLUME ["/data"]

EXPOSE 8088

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8088/health', timeout=3)" || exit 1

# Reads EMPORIA_RELAY_PORT itself and binds 0.0.0.0 — see src/emporia/relay_server.py:main()
CMD ["emporia-relay"]
