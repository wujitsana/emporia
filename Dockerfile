# Emporia relay — a single container serving the REST/WS API on one port (8088).
#
# No dashboard (P196): the dashboard runs next to the agents' keys — a Hermes
# dashboard tab or the local console — never as a public page on a relay. The
# relay's operator watches it the same way, as its operator agent; see
# docs/RUNBOOK.md § Watching a relay you host.
#
#   docker build -t emporia-relay .
#   docker run -p 127.0.0.1:8088:8088 -v emporia-data:/data --env-file .env \
#     -v "$PWD/secrets:/run/secrets:ro" emporia-relay
#
# See docker-compose.yml for a ready-to-run stack (env file, volume, optional profiles).

# ── Relay runtime ─────────────────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

RUN pip install --no-cache-dir uv

WORKDIR /app

# Install Python deps first (better layer caching on code-only changes)
# README.md: pyproject names it as the package readme, and building the
# project fails without it (P106 — the image had not built since init).
COPY pyproject.toml uv.lock README.md ./
COPY src/ ./src/
# P207: `emporia signer add` (the signer's admin service) reads profiles with the installer's helpers.
COPY installer/ ./installer/
# --extra x402 (P106): the escrow operator (P96-P99) signs and ABI-encodes on
# the relay itself, so the image needs eth-account/eth-abi. Without them a
# relay with EMPORIA_ESCROW_* set failed at the first /fund.
RUN uv sync --frozen --no-dev --extra x402


# Ed25519 keys, SQLite DB, and JSONL audit logs must survive container restarts.
# P200: secrets come as files in ./secrets, mounted read-only at /run/secrets —
# never environment variables, which `docker inspect` shows to anyone with
# Docker. The relay reads them as root at start, then runs as `emporia`.
# P203: the same image runs the signer (`emporia signer serve`); its socket folder,
# shared with the Hermes container as a volume, starts out the signer's.
RUN useradd --system --uid 10001 --home-dir /data --shell /usr/sbin/nologin emporia \
    && mkdir -p /data /run/emporia-signer && chown emporia:emporia /data /run/emporia-signer \
    && chmod 700 /data && chmod 755 /run/emporia-signer
ENV EMPORIA_KEYS_DIR=/data/keys \
    EMPORIA_DB_PATH=/data/emporia.sqlite3 \
    EMPORIA_SECRETS_DIR=/run/secrets \
    EMPORIA_RUN_AS=emporia \
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
