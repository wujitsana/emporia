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

## Lanes 2 and 3 — the Stripe rails

Not here yet. P45 shipped the x402 lane; the Stripe PaymentIntent and MPP/SPT lanes land in P46 and
this runbook grows a section each when they do. Documenting them ahead of the code is the exact
defect this repo has now recorded twice (P43's undocumented capability, P45's F2 — a rail described
as "live" that had no client), so the sections appear with the tests, not before.

---

## What this tier does and doesn't prove

**Does:** that a credential this repo produces is accepted by an implementation that did not come
from this repo, and that a tampered one is refused by the same implementation. That is the only
claim a mock can never support, and it is the reason the tier exists.

**Doesn't:** anything about mainnet, about facilitators other than the one configured, or about
sustained load. It also doesn't cover the settlement-retry gap recorded in `SECURITY.md` — x402 has
no idempotency key on `/settle`, so a relay crash between settlement and `record_payment()` is still
a real hole and no test here reproduces it.
