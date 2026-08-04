# Guardrails & relay dotenv (session notes)

## Defaults (installer / bootstrap)

- `EMPORIA_GUARDRAILS_MODE=enforce` (unless `--no-guardrails`)
- `EMPORIA_NEMO_GUARDRAILS_ENABLED=0` always, regardless of whether `NVIDIA_API_KEY` was resolved (code default `0` in `guardrails.py`). Pre-fix, the installer auto-set `1` from key presence alone — including a leftover hackathon-sponsor key nobody meant to keep paying for, on every routine re-run. A resolved key is still saved to the profile `.env`, so `EMPORIA_NEMO_GUARDRAILS_ENABLED=1` there afterward is a deliberate manual opt-in, not something the installer will ever set again.
- Stripe: `STRIPE_SECRET_KEY` written only when resolved; never `STRIPE_SECRET_KEY=""` in new profiles
- `emporia/.env.example` documents the pattern

## Provider secret resolution

Functions: `collect_upstream_env`, `resolve_provider_secrets`, `_print_provider_status` in `installer/install.py`.

Upstream walk: parent dirs of profile (up to 14 levels) + `harvest_config_environment` + `emporia/.env` under profile.

Flags: `--non-interactive`, `--nvidia-api-key`, `--stripe-secret-key`, `--stripe-profile-id`.

`_apply_guardrails_env(env_block, nvidia_api_key=...)` always sets `EMPORIA_NEMO_GUARDRAILS_ENABLED="0"` — `nvidia_api_key` only controls whether `NVIDIA_API_KEY` itself gets saved to `env_block`, not the enabled flag.

## relay_server.py load order

1. Merge profile `.env` (ancestor dir containing `config.yaml`)
2. Merge emporia repo `.env` (`pyproject.toml` + `src/emporia`)
3. Import `emporia.engine.guardrails` (reads env once at import)

## NVIDIA_API_KEY

Hermes stores keys in `config.yaml` `environment:` for the agent/MCP child process. **uvicorn relay** does not read that block unless copied to profile `.env` or exported in the shell starting `relay/server.py`.

**Confirmed the hard way (N13-3):** editing `EMPORIA_NEMO_GUARDRAILS_ENABLED` in `config.yaml` alone does *not* change relay/pytest behavior — `_load_profile_dotenv()` (`relay_server.py`) reads the profile-root `.env` file directly, a separate artifact that isn't kept in sync with `config.yaml`'s `env:` block by anything automatic. When flipping this value, check *and edit* both, or a `config.yaml` change can look applied while the actual running process (and every `pytest` run, which imports `relay_server` too) keeps using the stale `.env` value.

## Verification

```bash
curl -s http://localhost:8088/safety/stats | jq '.nemo_guardrails_enabled, .nemo_guardrails_model'
```

Expect `true` and `nvidia/nemotron-mini-4b-instruct` when enabled and relay restarted after code/env changes.