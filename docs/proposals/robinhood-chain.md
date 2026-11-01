# Robinhood Chain support

Status: research only, nothing built (2026-11-01)

## What it is

- An Ethereum layer 2 built on the Arbitrum Orbit stack. It is EVM
  compatible, settles to Ethereum, and uses ETH as the gas token.
- Testnet is live. It settles to Ethereum Sepolia.
  - Chain id 46630, so CAIP-2 `eip155:46630`.
  - Public RPC: https://rpc.testnet.chain.robinhood.com
  - Explorer: https://explorer.testnet.chain.robinhood.com
  - Faucet: https://faucet.testnet.chain.robinhood.com
- Mainnet: one search source gave chain id 4663 (`eip155:4663`). I did not
  confirm it against Robinhood's own docs, and the sources disagree on the
  mainnet explorer. Treat mainnet as unverified until read from
  https://docs.robinhood.com/chain/connecting.
- Stablecoin: sources say the canonical stablecoin is USDG (Global Dollar),
  not Circle USDC. I found no official USDC address on the testnet.
- A third-party package README (hood402) lists a testnet USDG address,
  `0x7E955252E15c84f5768B83c41a71F9eba181802F`, and says it checked that
  `transferWithAuthorization` (EIP-3009) exists on it. This is not from
  Robinhood. Verify on the explorer before use.

## Does x402 apply

Yes, in principle. x402 on EVM needs only an EIP-3009 token and a
facilitator that can submit the transfer and pay gas. The chain is plain EVM,
and the USDG token reportedly has EIP-3009. Two gaps:

- The token is USDG, not USDC. Emporia's `networks.py` field is called `usdc`
  but already holds other tokens (Tempo's pathUSD), so this fits. The EIP-712
  `name` and `version` for the token must be read from the contract and put in
  `EMPORIA_X402_EXTRA_EIP155_46630`.
- Facilitator: I found only a third-party self-hostable one (hood402) that
  lists `robinhood` and `robinhood-testnet`. I could not read the Coinbase
  CDP network-support page (host blocked in this session), so I cannot say
  whether CDP or x402.org support it. Assume not until checked. Emporia can
  point at any facilitator via its facilitator URL setting, so a self-hosted
  one is an option, with the usual trust cost.

## Wallet keys

No new key type. Operator and agent EVM keys are the same secp256k1 keys on
every EVM chain, so the same address works on Robinhood Chain. What changes:

- A new network id in the allowed-networks lists (agent wallet limits, relay
  `EMPORIA_X402_NETWORKS`).
- Funding: the payer needs the stablecoin; the operator key needs a little ETH
  only if it submits transactions itself (escrow). x402 payers do not need gas.
- `operator_signer.MAINNET_NETWORKS` lists mainnet ids that gate hot-key use.
  Add 4663 there when mainnet is supported. Networks not in `networks.py` are
  already treated as mainnet by `is_testnet()`, which is the safe default.

## What to change in Emporia (file list)

Precedent: Tempo was added the same way (`eip155:42431`).

- `src/emporia/networks.py`: add a `Network("eip155:46630", ..., testnet=True,
  usdc=<USDG address>, rpc, explorer, faucet)` entry (and mainnet entry later),
  plus short aliases in `ALIASES` (for example `robinhood-testnet`,
  `robinhood`).
- `src/emporia/cli.py`:
  - `_BASE_SEPOLIA` preset (about line 3609) is the model for a new preset
    block with `EMPORIA_X402_NETWORKS`, `_ASSET_`, `_EXTRA_`, `_RPC_` for
    `EIP155_46630`.
  - Testnet preset branch around line 3647 (`elif net == "eip155:42431"`)
    needs an `elif` for the new chain.
  - Error text at line 4698 and help strings (about 5399, 5450) list network
    names.
- `src/emporia/escrow_chain.py`: `PUBLIC_RPC` (line 175) needs `46630`. Escrow
  contract deployment on this chain would also be needed if escrow is wanted.
- `src/emporia/operator_signer.py`: `MAINNET_NETWORKS` (mainnet only).
- `src/emporia/payments.py` and `agent_wallet.py`: no change expected (generic
  `eip155:` handling); check the EIP-712 domain code uses `extra` name/version.
- `dashboard/src/paymentFormat.ts` (and its test): network labels and explorer
  links.
- Docs: `README.md` env tables, `docs/payments.md`, `docs/testnet.md`,
  `docs/proposals/payment-options.md`, skills `SKILL.md` network mentions.
- Tests: `tests/test_tempo.py` and `tests/test_wallet_setup.py` are the
  patterns; add cases for the alias, `is_testnet`, and preset output.

## Blocked on / open questions

- A facilitator that supports the chain. CDP and x402.org support unconfirmed.
  Without one, we would run or trust a self-hosted one.
- Official token address and EIP-3009 confirmation from Robinhood docs or the
  explorer (current evidence is a third-party README). Also the token's
  EIP-712 domain name and version.
- Whether testnet USDG can be obtained from a faucet (the faucet may give
  only ETH).
- Mainnet chain id, explorer, and launch status.
- Escrow contract deployment on this chain, if escrow is in scope.
- Robinhood's terms for using the chain from agents (not researched).

## Sources

- https://docs.robinhood.com/chain/connecting
- https://docs.chainstack.com/docs/robinhood-tooling
- https://docs.orbitflare.com/robinhood-chain.md
- https://developers.bitgo.com/docs/robinhood-chain.md
- https://socket.dev/npm/package/hood402
- https://socket.dev/npm/package/hood402-facilitator
- https://agent402.tools/guides/usdg-payments-robinhood-chain
- https://docs.cdp.coinbase.com/x402/network-support (not readable here)
