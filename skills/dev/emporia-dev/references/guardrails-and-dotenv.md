# Guardrails & relay dotenv (session notes)

## Defaults (installer / bootstrap)

- `EMPORIA_GUARDRAILS_MODE=enforce` (unless `--no-guardrails`). The guardrails are a local regex
  firewall only. P151 removed the hackathon's optional NVIDIA-hosted semantic check (it sent text
  to a third party, cost paid calls per write, and could not read sealed content). Setting a
  profile up again drops its old `EMPORIA_NEMO_GUARDRAILS_ENABLED` line; an `NVIDIA_API_KEY` is
  left alone, since it may be the key an agent's own model runs on.
- Stripe: `STRIPE_SECRET_KEY` written only when resolved; never `STRIPE_SECRET_KEY=""` in new profiles
- `emporia/.env.example` documents the pattern

## Provider secret resolution

Functions: `collect_upstream_env`, `resolve_provider_secrets` (the relay's Stripe settings),
`_print_provider_status` in `installer/install.py`.

Upstream walk: parent dirs of profile (up to 14 levels) + `harvest_config_environment` + `emporia/.env` under profile.

Flags: `--non-interactive`, `--stripe-secret-key`, `--stripe-profile-id`.

## relay_server.py load order

1. Merge profile `.env` (ancestor dir containing `config.yaml`)
2. Merge emporia repo `.env` (`pyproject.toml` + `src/emporia`)
3. Import `emporia.engine.guardrails` (reads `EMPORIA_GUARDRAILS_MODE` once at import)

## config.yaml vs .env

Hermes stores keys in `config.yaml` `environment:` for the agent/MCP child process. The **relay**
does not read that block: `_load_profile_dotenv()` reads the profile-root `.env` directly
(N13-3). When changing a relay setting, edit the `.env`, or the running relay (and every
`pytest` run, which imports `relay_server` too) keeps the old value.

## Verification

```bash
curl -s http://localhost:8088/safety/stats | jq '.guardrails_mode, .guardrail_blocks'
```
