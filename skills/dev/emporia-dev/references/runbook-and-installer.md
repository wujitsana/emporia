# Runbook & installer — reference

Repo sources: **`docs/RUNBOOK.md`**, **`README.md` § Operations**, **`installer/install.py`** module docstring.

## Installer flags (ops vs profiles)

| Flag | Effect |
|------|--------|
| `--install-profile` | Patch current profile `config.yaml`, MCP env, skill symlink; localhost → seed |
| `--create-profile NAME` | New Hermes profile from template |
| `--bootstrap-test` | Four demo profiles + restore operator dashboard `.env.local` + seed |
| `--local-demo` | `--build-dashboard` + `--start-relay` + `--seed-only` (no profiles) |
| `--seed-only` | `scripts/seed_demo_relay.py` (starts relay if needed) |
| `--start-relay` | `uv sync` + background uvicorn via `local_relay.py` |
| `--build-dashboard` | `npm install` + `npm run build:embedded` → relay `/ui/` |

Wrapper: **`scripts/run_local_demo.sh`** → `--local-demo`.

## Bootstrap vs seed

| | `--bootstrap-test` | `--seed-only` |
|--|------------------|---------------|
| Hermes profiles | Creates alpha, beta, nemotron_strategist, stripe_escrow_bot | No |
| Relay | Starts if down | Starts if down |
| DB content | Seeds | Seeds |

Re-seed without touching profiles: **`--seed-only`** only.

## Hermes self-install sequence

1. `cd emporia` (inside profile tree so installer finds `config.yaml`)
2. `python installer/install.py --install-profile --relay-url http://127.0.0.1:8088`
3. User runs **`/reload-mcp`**
4. Load **`emporia`** skill for MCP tool flows

## Documentation edits

When updating install/runbook copy for judges or operators:

- Use **operator**, **judge**, **Hermes agent** — not **“Admin”** as a person/role.
- Stripe field names like `stripe_mpp_admin_notice` are API/product labels; describe them as **operator-facing notices**.

## Verify stack (no curl|python pipes)

```bash
cd emporia
.venv/bin/python scripts/_inspect_sessions.py
.venv/bin/python scripts/_test_replay_fens.py
```

Health: `chess_lib: true` after relay restart post-`uv sync`.