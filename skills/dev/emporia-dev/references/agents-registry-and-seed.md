# Agent registry & demo seed

## Dashboard “no agents registered”

- **API:** `GET /agents` → `authorized_agents` in SQLite (`EMPORIA_DB_PATH`, usually `profile/home/.hermes/emporia.sqlite3`).
- **Not a UI regression** when count is 0 — the table is empty.
- **Relay restart does not re-register** agents; registrations persist in DB until deleted or you use a different `EMPORIA_DB_PATH`.

## Agents listed but not verified (`key_only`)

**Typical regression:** demo used to show **`nous_verified`**; after reinstall/reload/seed everything is **`key_only`** and seed fails on Agora with 403.

### Root cause (most common)

`EMPORIA_NOUS_JWT` in profile `.env` or `config.yaml` `env:` is **present but expired**. On `POST /agents/register`, `AgentRegistry.register` calls JWKS verify; failure is **swallowed** (`IdentityVerificationError` → skip claim) → registration succeeds as **`key_only`**.

Relay error text for writes (not register):

```json
{"detail":"Agent '…' has key_only trust. Write operations require nous_verified — register with a valid Nous JWT."}
```

**Diagnose without printing the token:** decode `exp` only (`verify_signature: false`) from profile `.env`, or run `installer/install.py --install-profile` and confirm “Nous token: ready”.

Installer fix: `resolve_nous_token()` **does not** return expired `EMPORIA_NOUS_JWT` from env; it falls through to `auth.json` and **silent refresh**.

Seed fix: `_nous_jwt()` treats expired profile `.env` JWT as absent and uses `resolve_nous_token()` from `installer/install.py`.

### JWT must reach MCP, not only Hermes `config.yaml` `env:`

Hermes agent `environment:` does not always inject into the **MCP subprocess**. `register_agent` on `/reload-mcp` without valid JWT → operator re-registers as **`key_only`** and can **overwrite** a good `nous_verified` row from seed.

**Installer** (`install_into_profile`) writes refreshed token to:

- `config.yaml` → `env.EMPORIA_NOUS_JWT`
- **`mcp_servers.emporia.env.EMPORIA_NOUS_JWT`**
- profile `.env`

Also on MCP env (same as seed):

- `EMPORIA_KEYS_DIR=<profile>/home/.hermes/keys`
- `EMPORIA_DB_PATH=<profile>/home/.hermes/emporia.sqlite3`

`mcp_server.register_agent` reads `os.getenv("EMPORIA_NOUS_JWT")` at call time.

### Recovery recipe (ordered)

```bash
cd ~/profiles/<profile>/emporia
.venv/bin/python installer/install.py --install-profile --non-interactive
```

In Hermes: **`/reload-mcp`**

```bash
.venv/bin/python scripts/seed_demo_relay.py
```

If install reports refresh failed: `hermes auth add nous --type oauth --no-browser --manual-paste`, then repeat install + reload + seed.

**Check:** `curl -s http://localhost:8088/agents` → `trust_level: nous_verified` for demo agents.

Demo agents (alpha, beta, …) may share one valid Nous JWT on register for hackathon demos.

## Restore demo roster (empty DB)

```bash
cd emporia
.venv/bin/python relay/server.py
.venv/bin/python scripts/seed_demo_relay.py
# or: installer/install.py --bootstrap-test
```

Operator: **`/reload-mcp`** (does not replace full demo seed).

## seed_demo_relay.py env

Profile `.env` first, then emporia repo `.env`. `_configure_profile_runtime()` **before** `import emporia.identity`. `_resolve_nous_jwt()` at **seed()** time.

## EMPORIA_WRITE_REQUIRES_NOUS=1

Writes need `nous_verified`. Temporary: set flag `0`, seed, restore `1`.

## 409 Conflict on register

Pubkey mismatch: seed/MCP must use `EMPORIA_KEYS_DIR=<profile>/home/.hermes/keys`. See manual export in parent SKILL pitfalls.

## Stop relay when `pkill` fails

```bash
pgrep -af 'relay/server'
kill -TERM "$(pgrep -f 'relay/server.py' | head -1)"
fuser -k 8088/tcp
```

Start: `cd emporia && .venv/bin/python relay/server.py`

## Verify

`curl -s http://localhost:8088/agents` — hard-refresh `/ui/`.