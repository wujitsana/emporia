# Stripe / MPP verification

Class-level checklist for "is our Stripe MPP set up properly?" on an Emporia relay.
Migrated from the retired standalone `emporia-stripe-mpp` skill — load this doc (part of
**emporia-dev**) instead of a separate skill.

## When this applies

- User asks to test Stripe, MPP, escrow, SPT, or `stripe_enabled` on the relay.
- `relay_payment_info` shows only `free` rails but user says the key is in `.env`.

Also load the profile-local **stripe-link-cli** skill for wallet/auth steps. For non-Stripe MPP
merchants use **mpp-agent**; Emporia's fiat path is Stripe + Link.

## Working rules

1. **Do not** `read_file` profile `.env` or print `STRIPE_SECRET_KEY`. Assume secrets are
   configured unless health proves otherwise.
2. Prefer relay/MCP tools (`relay_payment_info`, `terminal` for pytest/link-cli/health only) —
   avoid ad-hoc key greps or balance checks as the first move.
3. Follow the verification steps below before inventing new probes.
4. Optional Stripe API ping: env in terminal without logging the key; **Permission denied** on
   `/v1/balance` with `rk_live_*` is common — use relay `stripe_enabled` + 402 as ground truth.

## Procedure

### 1. Relay rails (MCP)

```
relay_payment_info
```

Pass: `stripe_enabled: true` and payment rails beyond `free`.
Fail: only `free` → relay process lacks non-empty `STRIPE_SECRET_KEY` (see pitfalls).

When key is set but **`mpp` is missing**, also check **`stripe_mpp_admin_notice`** on `/health`
and `relay_payment_info` — operator text (no secrets) explaining why auto-discover failed and
what to paste from Dashboard. Same text appears in installer **`WARNING: ADMIN:`** and relay
startup logs after code deploy + restart.

**Rail inventory (relay `payment_rails`):**

| Rail | Requires | Meaning |
|------|----------|---------|
| `free` | — | Always on |
| `stripe_pi` | Non-empty `STRIPE_SECRET_KEY` | PI / escrow; `stripe_enabled: true` |
| `mpp` | Key **+** valid `STRIPE_PROFILE_ID` (`profile_…` / `profile_test_…`) | Stripe MPP seller; `mpp_enabled: true`, `stripe_profile_ready: true` |

**Common gap:** key present, `payment_rails` = `["free","stripe_pi"]` only → **missing `mpp`**
because `STRIPE_PROFILE_ID` is empty. Fees/dashboard **MPP off** follows `stripe_profile_ready`,
not `stripe_enabled`.

**Auto-fill profile id:** `installer/install.py --install-profile` scans env files and (for
**`sk_*`** keys only) Stripe Account JSON via `emporia.stripe_profile_discovery`. **`rk_*`
restricted keys cannot auto-discover** (Account API 403) — one-time copy from Dashboard →
`STRIPE_PROFILE_ID` in profile `.env`, then install-profile + relay restart. CLI:
`--stripe-profile-id auto`. Detail: `references/stripe-profile-id.md`.

**Do not confuse with dashboard Profile "Stripe: not connected".** That label is
`get_agent_profile` → `has_stripe` (Stripe **Connect** `stripe_account_id` on the agent). Relay
can show `stripe_enabled: true` while Profile still says not connected. Fees tab KPI stripe
on/off uses relay health, not agent Connect.

### 2. MPP challenge code path (pytest)

From `emporia` repo root:

```
.venv/bin/python -m pytest tests/test_emporia.py::test_mpp_402_challenge_issued -q
```

Pass: `Payment` scheme, `method="stripe"`, `intent="charge"`.

### 3. Agent wallet (stripe-link-cli)

```
link-cli --version && link-cli auth status
```

Installed ≠ authenticated. For live `mpp pay`, run
`link-cli auth login --client-name "Hermes" --interval 5 --timeout 300` and wait for user
approval in the Link app.

### 4. Live 402 (MCP)

After `register_agent` for creator + joiner:

- `create_session` with `payment_mode: stripe_link`, `stake_per_participant: "1.00"`
- `join_session` as joiner **without** payment → expect 402, `protocol: emporia:v1+mpp`

If `create_session` returns 422 `creator_agent_id` required: Emporia MCP must send
`creator_agent_id` (not `creator_id`); user runs **`/reload-mcp`** after server code updates.

### 5. End-to-end pay

`link-cli mpp pay http://127.0.0.1:8088/sessions/{id}/join --method POST --data
'{"agent_id":"…"}'` (+ spend request per stripe-link-cli).

## Pitfalls

| Symptom | Cause | Fix |
|---------|--------|-----|
| Key "in .env" but `stripe_enabled: false` | Relay started before env fix; empty `STRIPE_SECRET_KEY=""`; or key only in `config.yaml` `environment:` (agent/MCP, not uvicorn) | Run `installer/install.py --install-profile` (copies from parent `.env` / config); remove empty STRIPE lines; restart relay with `set -a && . ../.env` |
| MCP still sends `creator_id` | Stale MCP subprocess | `/reload-mcp` after `mcp_server.py` patch |
| Balance API fails, health OK | Restricted Stripe key | Not a relay setup failure |
| User wants skills only | — | Do not dump key audits; run steps 1–3 minimum |
| Profile "Stripe not connected", health `stripe_enabled: true` | Agent has no Connect account (`has_stripe: false`) | Auto-provision on `register_agent` only when relay key is `sk_test_*`; re-register after test key, or attach `acct_…` via registry |
| Dashboard Fees stripe "off" but MCP health true | Wrong `VITE_RELAY_URL` or stale tab | Hard refresh; confirm dashboard points at same relay as MCP |
| `stripe_enabled: true` but **no `mpp` rail** / `mpp_enabled: false` | `STRIPE_SECRET_KEY` only; no `STRIPE_PROFILE_ID` | Set profile id (manual for `rk_*`); `install-profile`; restart relay; re-check `relay_payment_info` |
| Installer says restricted key, cannot auto-discover | `rk_live_*` / `rk_test_*` | Dashboard Machine payments / Agentic commerce → paste `STRIPE_PROFILE_ID`; not fixable by balance/account API probes |
| User asks "tell admin" when key present, no profile | — | Ensure `stripe_mpp_admin_notice` on `/health`; run `install-profile` (prints `WARNING: ADMIN:`); Fees → Payments banner after `build:embedded` + relay restart |

## Stop / start relay (Hermes `terminal`)

Stop (port free = down). If **`pkill` does not work** on the host, use PID or port:

```bash
pgrep -af 'relay/server'
kill -TERM "$(pgrep -f 'relay/server.py' | head -1)"
# or:
kill $(lsof -t -i:8088) 2>/dev/null
fuser -k 8088/tcp 2>/dev/null
```

Legacy pattern (may fail on some containers):

```bash
pkill -f 'uvicorn emporia.relay_server'
```

Verify: `curl -s http://127.0.0.1:8088/health` should fail or show updated `stripe_enabled`
after restart.

```bash
cd <profile>/emporia
set -a && . ../.env && set +a
.venv/bin/python -m uvicorn emporia.relay_server:app --host 0.0.0.0 --port 8088 --app-dir src
```

## Support detail

Session-specific notes: `references/stripe-mpp-setup.md`, `references/stripe-profile-id.md`.
For `nous_verified` vs `key_only` trust troubleshooting (separate from payments):
`references/agents-registry-and-seed.md`.
