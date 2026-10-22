# Testing the payment lanes against real infrastructure

Everything in the default test run is offline. That is deliberate — a red build should mean the code
is wrong, not that a faucet was empty — but it left a gap the audit passes kept recording rather
than closing: **no test had ever contacted a real facilitator**, so the x402 wire shape was known to
be self-consistent with the specification and unknown to be interoperable with anything.

This is the opt-in tier that closes it. It is marked, excluded from the default run and from CI, and
every test in it *skips* rather than fails when its configuration is absent.

```bash
uv sync --extra dev --extra x402
uv run pytest -m testnet -rs        # -rs prints why anything skipped
```

Nothing here is required to develop on Emporia. It is required before believing a lane works.

---

## The short way: `scripts/testnet.py`

Everything below is still true and worth reading once. For running the lanes, prefer the script — it
supplies the config rather than asking you to type it, and it will not let you point the harness at
mainnet:

```bash
python scripts/testnet.py --check     # what can run, and why the rest cannot
python scripts/testnet.py             # run every lane that is ready and free
python scripts/testnet.py --lane 1b   # just one
```

`--check` answers the questions the lanes actually depend on rather than whether a host resolves: it
asks the facilitator's `/supported` whether it serves the configured network and reports the schemes
it offers, and it makes a real `eth_blockNumber` call and reports the head block. A plain reachability
probe returned "ok (HTTP 403)" for the facilitator and "ok (HTTP 405)" for the RPC — both were the
probe's own request shape, and the same code would have called a genuine "your requests are blocked"
403 ready.

The chain id is checked against an allowlist of testnets and anything else is refused, exit code 1.
The rule below — *never point `EMPORIA_TESTNET_*` at mainnet* — was previously enforced by the reader
remembering it; a mistyped chain id looks exactly like an intended one at the point a transfer gets
signed.

Lanes that spend anything are opt-in: the settlement lane needs `EMPORIA_TESTNET_PAYER_KEY` and is
never selected by a bare run.

## Lane 1 — x402 on Base Sepolia

The public facilitator at `https://x402.org/facilitator` supports `exact` on `eip155:84532` at
x402Version 2 (check with `curl https://x402.org/facilitator/supported`). It is free and needs no
account.

```bash
export EMPORIA_TESTNET_FACILITATOR_URL="https://x402.org/facilitator"
export EMPORIA_TESTNET_NETWORK="eip155:84532"
export EMPORIA_TESTNET_ASSET="0x036CbD53842c5426634e7929541eC2318f3dCF7e"   # USDC, 6 decimals
```

That much runs two tests: the facilitator accepts a credential this repo signed, and it rejects one
tampered with after signing. **Neither needs funds** — `/verify` checks the signature, the
authorization window and the requirement match; it does not move money. The second test is the
control for the first: `isValid: true` means nothing unless `isValid: false` is reachable.

Without `EMPORIA_TESTNET_PAYER_KEY` those two fall back to the private key `0x11…11`. That key is
**publicly known** — its address (`0x19E7E376…ff2A`) is a well-worn testnet address that accumulates
other people's faucet drips, and at the time of writing it held ~0.87 testnet USDC that is not
yours. Signing a `/verify` request with it moves nothing and is fine. Never use it, or any other
shared key, on a path that reaches `/settle`: you would be spending someone else's balance. The
settlement test refuses to run without an explicit `EMPORIA_TESTNET_PAYER_KEY` for exactly that
reason, and pays to the payer's own address so the principal comes straight back.

Settlement is a third test and does need funds:

```bash
export EMPORIA_TESTNET_PAYER_KEY="0x..."   # a throwaway key holding Base Sepolia USDC
```

Fund it with Base Sepolia ETH (bridge or faucet) and testnet USDC from Circle's faucet
(<https://faucet.circle.com>, select Base Sepolia). The settlement test pays a **1-cent** room and
sends the payment to the payer's own address, so it costs gas and returns the principal rather than
leaking USDC to a third party on every run.

**Never point `EMPORIA_TESTNET_*` at mainnet.** The relay's own `EMPORIA_X402_FACILITATOR_URL` and
asset defaults are blank by design and must stay that way.

### Running the relay itself on the testnet lane

The relay's own config uses different variable names — the `EMPORIA_TESTNET_*` block above is the
test harness's, deliberately separate so a configured relay can't drag the test suite onto a network
it didn't intend. To run a live relay on this lane:

```bash
export EMPORIA_X402_ENABLED=1
export EMPORIA_X402_FACILITATOR_URL="https://x402.org/facilitator"
export EMPORIA_X402_NETWORKS="eip155:84532"
export EMPORIA_X402_ASSET_EIP155_84532="0x036CbD53842c5426634e7929541eC2318f3dCF7e"
export EMPORIA_X402_EXTRA_EIP155_84532='{"name": "USDC", "version": "2"}'
```

The `_EXTRA_` value is the token contract's EIP-712 domain, and it is not optional on EVM: without
it the challenge is well-formed and unsignable, so the network is excluded from `accepts` outright
rather than advertised as payable. Read it off the contract rather than copying it, if you are
configuring a network not listed here:

```bash
# name() and version() — 0x06fdde03 and 0x54fd4d50
cast call 0x036CbD53842c5426634e7929541eC2318f3dCF7e "name()(string)"    --rpc-url https://sepolia.base.org
cast call 0x036CbD53842c5426634e7929541eC2318f3dCF7e "version()(string)" --rpc-url https://sepolia.base.org
```

Then, as a creator, register a payout wallet before anyone can pay you:

```
POST /agents/{id}/x402-wallet   {"network_family": "evm", "address": "0x..."}
```

---

## Lane 1b — the chain reads, free and unfunded (P48)

Three tests read Base Sepolia directly, with no key, no gas and no money at risk. They exist because
P48 closes x402's settlement window by asking the chain what the facilitator can't be asked: EIP-3009
records a spent authorization permanently, so `authorizationState(authorizer, nonce)` answers "was
this payer charged?" after a crash.

```bash
export EMPORIA_X402_RPC_EIP155_84532="https://sepolia.base.org"
uv run pytest -m testnet -k "authorization or window" -rs
```

They assert against P45's own settlement, which is permanently on chain: its nonce reads as spent,
an arbitrary nonce reads as unspent (the control — an answer of True for everything would look
identical without it), and a recent `AuthorizationUsed` log from somebody else's transfer is
recovered by authorizer and nonce alone.

This is the cheapest honest integration test in the repo: a counterparty nobody here wrote, no
funding, and it runs in about four seconds. It found two things the offline tests could not — Base's
public RPC caps `eth_getLogs` at 10,000 blocks, so a "not found" log is a bound and never evidence a
payment didn't happen; and `toBlock: "latest"` advances between calls, so a window sized at the cap
failed intermittently.

---

## Lane 2 — Stripe PaymentIntent (test mode)

```bash
export EMPORIA_TESTNET_STRIPE_KEY="sk_test_..."
```

Test mode only, and asserted rather than trusted: the skip guard checks the `sk_test_` prefix, so a
live key configured by accident skips the tier instead of creating real PaymentIntents.

This lane needed no new payer code — `POST /payments/create-intent` and the SDK's
`create_payment_intent()` already existed. Worth stating next to lane 1: x402 was missing a payer;
Stripe was missing only a test that talked to Stripe. What it turned out to be missing badly was a
working auto-confirm — `confirm_stripe_intent()` POSTed to
`/v1/test_helpers/payment_intents/{id}/confirm`, a route Stripe does not serve, on a path nothing
had ever executed (P46-1). The narrow `..._confirm_reaches_a_real_endpoint` test exists so that
regression fails about the endpoint rather than about a room three layers away.

Confirmation uses `pm_card_visa`, Stripe's always-succeeds test token. No card details exist
anywhere in this repo.

---

## Lane 3 — Stripe MPP / Shared Payment Tokens

```bash
export EMPORIA_TESTNET_STRIPE_KEY="sk_test_..."
export EMPORIA_TESTNET_STRIPE_PROFILE_ID="profile_test_..."   # Dashboard → Machine Payments
export EMPORIA_TESTNET_SPT="spt_..."                          # optional; minted automatically if unset
```

The token is minted by the harness from Stripe's own test helper
(`POST /v1/test_helpers/shared_payment/granted_tokens`, confirmed to exist), so this lane runs
unattended — no link-cli, no wallet. Set `EMPORIA_TESTNET_SPT` only to test a token a real wallet
produced.

SPTs are single-use and expire. A supplied one works exactly once; a second run with the same value
fails at Stripe, not here.

The credential travels as `Authorization: Payment <spt>`, which displaces the identity bearer token —
they share that header and cannot both be sent. That is why the relay treats `agent_id` as unproven
on this branch (`_require_caller_unless_mpp_payment`) and leans entirely on payment resource-binding.

---

## Lane `escrow` — x402 escrow holds for purchases (P99, spends testnet USDC)

A real relay subprocess against Base's Commerce Payments contracts (`AuthCaptureEscrow` v1.1.0 at
`0xf968…B19c`, `ERC3009PaymentCollector` at `0x8612…ab88`, same addresses on Base Sepolia and Base),
driven through the SDK:

- **A:** a buyer funds a $1.00 service purchase with an escrow hold, the seller delivers, the buyer
  confirms → the hold is captured: a fresh seller wallet receives 0.975 USDC, the fee receiver 0.025.
- **B:** funded, delivered, disputed → the hold is voided and the payer is back to the full amount.

Keys are passed as **file paths**, never as hex in the environment or shell history:

```bash
# Key files live in the profile's key directory (EMPORIA_KEYS_DIR, set in the
# profile .env by the installer -- e.g. <profile>/home/.hermes/keys).
export EMPORIA_TESTNET_OPERATOR_KEY_FILE="$EMPORIA_KEYS_DIR/relay_operator.secp256k1"   # needs a little Base Sepolia ETH
export EMPORIA_TESTNET_PAYER_KEY_FILE="$EMPORIA_KEYS_DIR/testnet_payer.secp256k1"       # needs >= 2 test USDC, no ETH
export EMPORIA_TESTNET_FEE_RECEIVER=0x...      # optional; a fresh address when unset
python scripts/testnet.py --lane escrow
```

Create the operator key with `python -m emporia.operator_signer generate` (it refuses to write anywhere
git would track it). Fund it from a Base Sepolia faucet — check the network: Ethereum Sepolia ETH is
on a different chain and has to be bridged. First run passed 2026-10-22; the transaction hashes are in
SECURITY.md's P96–P99 section.

## What this tier does and doesn't prove

**Does:** that a credential this repo produces is accepted by an implementation that did not come
from this repo, and that a tampered one is refused by the same implementation. That is the only
claim a mock can never support, and it is the reason the tier exists.

It found one immediately. `confirm_stripe_intent()` had been POSTing to a Stripe route that does not
exist since before the hackathon, on a path with no test coverage of any kind, and a mocked
`httpx.AsyncClient.post` accepts every URL equally — the defect was invisible to any amount of
additional mocking and obvious the moment something real was asked.

**Doesn't:** anything about mainnet, about facilitators other than the one configured, or about
sustained load. The settlement-retry gap this section used to list here is closed (P48) and
reproduced by an offline test, not by this tier; what lane 1b adds is that the chain reads P48 relies
on address what they claim on a live contract.

**Run status, honestly:** lane 1 has been executed against the live facilitator (P45), and lane 1b
against Base Sepolia (P48) — 3 passed. Lanes 2 and 3
are written and have never been run — no Stripe test key was available in the environment where they
were built, so they have only ever skipped. Treat their first real run as part of the work, not as a
formality: the P46-1 URL fix is pinned offline by an asserted literal and confirmed against Stripe's
routing table, but nothing has yet watched a PaymentIntent actually confirm.
