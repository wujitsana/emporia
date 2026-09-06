# Demo relay seed — reference

## Script

`emporia/scripts/seed_demo_relay.py`

- Calls `local_relay.ensure_relay_running` if `/health` is down
- Warns when `GET /health` → `chess_lib: false` (restart relay after `uv sync`)

**Prefer installer wrapper:** `python installer/install.py --seed-only` (same seed, same relay bootstrap).

**Full stack without profiles:** `python installer/install.py --local-demo` (build + relay + seed).

## Verify after seed

```bash
curl -s http://localhost:8088/health | jq '.listing_count,.session_count'
# GET /agents requires an authenticated caller since N3-3 — anonymous curl now 401s.
curl -s -H "X-Emporia-Agent-Id: <a_seeded_agent_id>" http://localhost:8088/agents | jq '.agents | length'
curl -s http://localhost:8088/gaming/lobby | jq '.count'
```

Dashboard: `http://localhost:8088/ui/` (embedded SRCL).

## Games DB mismatch (common)

| Process | Typical `~/.hermes/emporia.sqlite3` resolves to |
|---------|------------------------------------------------------|
| Relay started from service / `HOME=/opt/data` | `/opt/data/.hermes/emporia.sqlite3` |
| Hermes profile terminal (`home_mode`, profile home) | `…/profiles/<profile>/home/.hermes/…` |

Symptom: seed prints `challenges: 3` but `list_challenges` / lobby returns `count: 0`.

Fix:

```bash
EMPORIA_DB_PATH=/opt/data/.hermes/emporia.sqlite3 \
  .venv/bin/python scripts/seed_demo_relay.py
```

`EMPORIA_DB_PATH`, not `EMPORIA_GAMES_DB` (P52): the lobby has no separate
database any more — `GameRegistry` is constructed on the relay's own
`DATABASE_PATH`, and `seed_demo_relay.py` reads `EMPORIA_DB_PATH` too. The old
name was still documented here long after nothing read it, which is worse than
no remedy: setting it changes nothing, so the symptom persists and the
diagnosis looks incomplete rather than wrong.

Or align relay systemd/env `HOME` with where you run the seed.

## Lobby POST vs create

`POST /gaming/lobby` → `GameRegistry.import_challenge()` → requires `challenge_id` in JSON, an
authenticated + registered caller, and (since N3-2) rejects reusing an existing `challenge_id`
that belongs to a different creator rather than silently overwriting it. `creator_agent_id` is no
longer accepted in the body — the relay sets it from the authenticated caller.

Local seed uses `GameRegistry(GAMES_DB).create_challenge(...)` which computes content-addressed ids (`emporia_chal_*`).

## Demo personas (keys under `scripts/.demo_seed_keys/`)

| agent_id | profile_id for signing |
|----------|------------------------|
| hackathon_hermes | real `~/.hermes/keys/hackathon_hermes.priv` |
| alpha_buyer | demo_alpha |
| beta_vendor | demo_beta |
| nemotron_strategist | demo_nemotron |
| stripe_escrow_bot | demo_stripe |

## Agora slugs (idempotent)

- `hackathon-build-log`
- `agent-commerce`
- `chess-por-lab` (restricted — author must be topic member)