# STRIPE_PROFILE_ID — enable the `mpp` rail

Canonical checklist: `references/stripe-mpp-verification.md`.

## Symptom

`relay_payment_info` (or `/health`) shows:

- `stripe_enabled: true`
- `payment_rails`: `["free", "stripe_pi"]` — **`mpp` missing**
- `mpp_enabled: false`, `stripe_profile_ready: false`, `stripe_profile_id: ""`

Secret key alone is intentional in code: PI works; MPP seller needs a Machine Payments
**profile id**.

## Operator alert (`stripe_mpp_admin_notice`)

When Stripe secret is configured but profile id is not, the relay exposes a **single
admin-facing string** (never includes secrets):

| Surface | Field / output |
|---------|----------------|
| `GET /health` | `stripe_mpp_admin_notice` |
| MCP `relay_payment_info` | same (proxied from health) |
| Relay startup | `emporia.relay` WARNING log |
| `install.py --install-profile` | `WARNING: ADMIN: …` in `_print_provider_status` when Stripe on, profile missing |
| Dashboard Fees → Payments | amber banner when health includes notice |

Message distinguishes **`rk_*`** (cannot auto-fetch; Dashboard copy required) vs **`sk_*`** (env
+ Account API tried, still no id).

**Relay must be restarted** after deploying this field; stale uvicorn will omit
`stripe_mpp_admin_notice` until restart.

## Automated resolution (installer)

On `install.py --install-profile`, `resolve_provider_secrets()` calls
`emporia.stripe_profile_discovery.resolve_stripe_profile_id()` when Stripe key exists but
profile id does not:

1. Scan `STRIPE_PROFILE_ID=` in profile `.env`, ancestor `.env`, `emporia/.env`
2. Merge `config.yaml` `env:` / `environment:` for provider keys
3. For **`sk_test_` / `sk_live_`** (not `rk_`): `GET /v1/account` with several `Stripe-Version`
   preview strings; walk JSON for `profile_…` / `profile_test_…`

Success prints `STRIPE_PROFILE_ID: auto-discovered (env_file|stripe_api)` and writes into
profile `config.yaml` / `.env`.

**`rk_*` restricted keys:** step 3 is skipped; installer + health return admin notice. No
public Stripe list-profiles API works with restricted keys in practice.

## After user changes Stripe key scope (retry)

User may widen **restricted key** permissions in Dashboard and ask to "try again" without
pasting `STRIPE_PROFILE_ID`.

1. Re-run: `installer/install.py --install-profile --non-interactive` (re-harvests keys from
   profile/parent `.env` and `config.yaml`).
2. If they **rotated** the restricted key, ensure the **new** `rk_*` or `sk_*` is in profile
   `.env` as `STRIPE_SECRET_KEY` — harvest does not read Dashboard-only changes.
3. Expect: **`rk_*`** may gain scopes such as **Payment Intents list (200)** while **Account
   (403)** remains — auto-discover still **fails**; `discover_note: restricted_key`.
4. **Widening scope ≠ Machine Payment profile id** — `STRIPE_PROFILE_ID` is still a separate
   Dashboard copy unless using full `sk_*` + Account JSON contains `profile_…`.
5. Surfaces to tell admin: installer `WARNING: ADMIN:`, `/health` → `stripe_mpp_admin_notice`,
   MCP `relay_payment_info`, Fees → Payments (after relay restart + `build:embedded`).

Pass criteria unchanged: `payment_rails` includes **`mpp`**, `stripe_profile_ready: true`,
notice **null**.

## Manual one-time (typical for `rk_live_*`)

1. Stripe Dashboard → **Machine payments** or **Agentic commerce** → copy Payment profile ID
   (`profile_…` or `profile_test_…`)
2. Add to `profiles/<name>/.env`: `STRIPE_PROFILE_ID=profile_…`
3. `cd emporia && .venv/bin/python installer/install.py --install-profile --non-interactive`
4. Restart relay on `:8088`
5. `relay_payment_info` → `mpp` in `payment_rails`, `stripe_profile_ready: true`,
   `stripe_mpp_admin_notice: null`

Hackathon demos often use `sk_test_…` + `profile_test_…` so installer discovery or bootstrap
seed paid MPP sessions can succeed.

## CLI

`--stripe-profile-id <id>` explicit override; `--stripe-profile-id auto` forces discover path
(ignores stale empty pick).

## Do not use as proof

- `/v1/balance` 403 with `rk_*` — unrelated to profile id
- Profile dashboard **Stripe: not connected** — Connect `has_stripe`, not relay `mpp` rail
