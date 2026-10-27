# Emporia relay — a single container serving the REST/WS API on one port (8088).
#
# The relay serves no dashboard (P196). The image also carries the local
# console and its dashboard (P197), run as a separate service only when asked
# — `docker compose --profile console up -d` in a stack `emporia relay host`
# wrote — listening on the server's loopback, for its operator over SSH, with
# the operator agent's keys mounted read-only. See docs/RUNBOOK.md § Watching a
# relay you host.
#
#   docker build -t emporia-relay .
#   docker run -p 8088:8088 -v emporia-data:/data --env-file .env emporia-relay
#
# See docker-compose.yml for a ready-to-run stack (env file, volume, optional profiles).

# ── The console's dashboard (P197) ────────────────────────────────────────────
FROM node:20-slim AS console-dashboard
WORKDIR /build/dashboard
COPY dashboard/package.json dashboard/package-lock.json* ./
RUN npm ci
COPY dashboard/ ./
RUN npm run build:console

# ── Relay runtime ─────────────────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

RUN pip install --no-cache-dir uv

WORKDIR /app

# Install Python deps first (better layer caching on code-only changes)
# README.md: pyproject names it as the package readme, and building the
# project fails without it (P106 — the image had not built since init).
COPY pyproject.toml uv.lock README.md ./
COPY src/ ./src/
# --extra x402 (P106): the escrow operator (P96-P99) signs and ABI-encodes on
# the relay itself, so the image needs eth-account/eth-abi. Without them a
# relay with EMPORIA_ESCROW_* set failed at the first /fund.
RUN uv sync --frozen --no-dev --extra x402

# P197: for the console service only — the relay never serves it.
COPY --from=console-dashboard /build/dashboard/dist-console ./dashboard/dist-console


# Ed25519 keys, SQLite DB, and JSONL audit logs must survive container restarts.
ENV EMPORIA_KEYS_DIR=/data/keys \
    EMPORIA_DB_PATH=/data/emporia.sqlite3 \
    EMPORIA_LOG_DIR=/data/logs \
    EMPORIA_RELAY_PORT=8088 \
    EMPORIA_RELAY_HOST=0.0.0.0 \
    PATH="/app/.venv/bin:${PATH}"
VOLUME ["/data"]

EXPOSE 8088

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8088/health', timeout=3)" || exit 1

# Reads EMPORIA_RELAY_PORT and EMPORIA_RELAY_HOST (0.0.0.0 in a container) — see src/emporia/relay_server.py:main()
CMD ["emporia-relay"]
