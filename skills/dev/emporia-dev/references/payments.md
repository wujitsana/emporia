# Payments

The user-facing model is in `docs/payments.md`; the settings are in `docs/environment.md`. This file is what a
contributor must not break.

## Principles

- **The relay never holds funds or decides outcomes.** Payments are non-custodial (x402 direct to the payee's
  registered wallet, Tempo). The relay never creates a wallet or payment account for anyone and never makes,
  confirms, transfers or refunds a payment for anyone (the card rail was removed; docs/payments.md). Escrow is off in 2.1 (its operator key would decide who is paid); a hold from an older relay is an on-chain contract the payer can
  reclaim from.
- **Games carry no stakes.** Entry fees and deals are money; a game result never moves it
  (`tests/test_no_game_stakes.py`, `tests/test_mcp_stakes.py`).
- **Secrets stay with their owner.** The relay's operator key is the relay's, from the environment only; agents never
  hold it. An agent's wallet keys never appear in tool arguments, logs or answers.
- **Agent spending has limits and a person's yes**: per-payment and daily caps, testnet only unless the person
  unlocks mainnet from their own terminal, an approval above a threshold. A payment a client cannot ask about is
  refused, never made.
- **Test money only in tests and demos.** Say so wherever an amount is shown.

## Rails

- `free`; x402 (own `PAYMENT-SIGNATURE` credential path, not a `payment_mode`); Tempo; x402 escrow holds on
  the Commerce Payments contract (off in 2.1: `escrow_chain.RELAY_ESCROW_ENABLED` is a test-only switch, no environment setting turns it on).
- The relay advertises what it accepts on `/health` and in `money_info` (under `relay`).
- The payment-rail vocabulary on the wire (`payment_mode`) and the internal rail tag stored on every payment
  (`payments.rail`) are separate axes.

## Invariants worth knowing before you edit

- The relay rebuilds its own expected x402 requirement (network, asset, exact amount, the payee's registered
  address) and compares the client's `accepted` object field by field before it calls the facilitator.
- Settle at most once: an intent row is written before `/settle`; a retry with an open intent is resolved by
  reading the chain, never settled again. "Could not ask" is never "not paid".
- Escrow (off in 2.1): holds that failed to capture or void were retried by a reconcile loop; with escrow off the loop does nothing, and a payer reclaims an old hold after its expiry.
- A paid join commits membership before it settles, so an interrupted purchase is finished on the next call, once
  per payment.
- Fees: `fees.py` and `EMPORIA_FEE_BPS`; read the code for the split, not a doc.

## Testing payments

- x402: `pytest -m testnet` against a public testnet facilitator (`docs/testnet.md`); off by default.
- Everything else is mocked at the HTTP boundary. A forgotten mock must fail on authentication: `tests/conftest.py`
  pins dummy keys.
