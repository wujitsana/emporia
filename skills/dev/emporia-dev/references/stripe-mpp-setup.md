# Emporia — Stripe / MPP setup verification

Condensed operator notes. Canonical procedure: `references/stripe-mpp-verification.md`.

## Two "Stripe" signals (common user confusion)

| UI / API | Field | Meaning |
|----------|--------|---------|
| `relay_payment_info`, `/health`, Fees → Payments KPI | `stripe_enabled` | Relay operator has non-empty `STRIPE_SECRET_KEY` — can issue 402, SPT, PI |
| Dashboard Profile → Connected agent | `has_stripe` | This **agent** has a Stripe Connect `stripe_account_id` (payouts) |
| `get_agent_profile` | `payment_rails` | What rails this agent may use; can list `stripe_spt`/`stripe_pi` even when `has_stripe` is false |

**MPP buyer path** needs relay `stripe_enabled` + **link-cli auth** (wallet), not Profile
`has_stripe`.

**Profile "Connect Stripe to enable paid sessions"** copy refers to seller payout Connect;
charging/buying can still work with relay Stripe on + link-cli.

## Relay env loading

- `relay_server.py` walks up from `src/emporia/` and loads the **first** `.env` found (usually
  the Hermes profile dir).
- An **empty** `STRIPE_SECRET_KEY=""` line above the real key blocked Stripe when loader used
  `setdefault` on empty values. Relay should skip empty assignments; still remove duplicate empty
  lines in profile `.env`.
- `stripe_enabled` reflects the **running** uvicorn process — restart after `.env` or loader code
  changes.

## MCP vs HTTP

- Relay expects `creator_agent_id` on `POST /sessions` (not `creator_id`).
- After `mcp_server.py` changes: **`/reload-mcp`** in Hermes (MCP subprocess is stale until
  reload).

## Stripe key types

| Signal | Meaning |
|--------|---------|
| `/v1/balance` permission error with `rk_live_*` | Restricted key; not proof relay is off |
| User: "it's in .env" | Do not inspect secrets; call `relay_payment_info`; restart relay if false |
| `sk_test_*` on relay at register | Auto `create_connected_account` → Profile `has_stripe` true for new agents |
| Key only, no `mpp` in `payment_rails` | Missing `STRIPE_PROFILE_ID` | See `references/stripe-profile-id.md` |
| `rk_*` + "automate profile id" | Stripe blocks account/profile listing | Manual Dashboard copy once |

## Repo skill symlink

The **emporia** agent skill lives under `emporia/skills/` in the repo (symlinked into the
profile). Patch that `SKILL.md` in-repo when adding operator-facing health notes; use
`references/stripe-mpp-verification.md` for full verification checklists.
