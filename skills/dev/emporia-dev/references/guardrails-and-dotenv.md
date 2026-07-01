# Guardrails & relay dotenv (session notes)

## Defaults (installer / bootstrap)

- `EMPORIA_GUARDRAILS_MODE=enforce` (unless `--no-guardrails`)
- `EMPORIA_NEMO_GUARDRAILS_ENABLED=1` **only** when `NVIDIA_API_KEY` was resolved; otherwise `0` (code default `0` in `guardrails.py`)
- Stripe: `STRIPE_SECRET_KEY` written only when resolved; never `STRIPE_SECRET_KEY=""` in new profiles
- `emporia/.env.example` documents the pattern

## Provider secret resolution

Functions: `collect_upstream_env`, `resolve_provider_secrets`, `_print_provider_status` in `installer/install.py`.

Upstream walk: parent dirs of profile (up to 14 levels) + `harvest_config_environment` + `emporia/.env` under profile.

Flags: `--non-interactive`, `--nvidia-api-key`, `--stripe-secret-key`, `--stripe-profile-id`.

`_apply_guardrails_env(env_block, nvidia_api_key=...)` sets `EMPORIA_NEMO_GUARDRAILS_ENABLED` from key presence, not unconditionally.

## relay_server.py load order

1. Merge profile `.env` (ancestor dir containing `config.yaml`)
2. Merge emporia repo `.env` (`pyproject.toml` + `src/emporia`)
3. Import `emporia.engine.guardrails` (reads env once at import)

## NVIDIA_API_KEY

Hermes stores keys in `config.yaml` `environment:` for the agent/MCP child process. **uvicorn relay** does not read that block unless copied to profile `.env` or exported in the shell starting `relay/server.py`.

## Verification

```bash
curl -s http://localhost:8088/safety/stats | jq '.nemo_guardrails_enabled, .nemo_guardrails_model'
```

Expect `true` and `nvidia/nemotron-mini-4b-instruct` when enabled and relay restarted after code/env changes.