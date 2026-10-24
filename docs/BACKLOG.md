# Backlog: everything deferred

Status: the one list of deferred work (2026-10-24). Each item was deferred on purpose: it is a
larger change, it needs a decision, or it is safer after manual testing. Designs live in the
linked proposals. [ROADMAP.md](../ROADMAP.md) is the older, hackathon-era record of open questions
by area (naming, security, payments, bounties, federation, interop, rooms, dashboard); items still
open there are not repeated here.

## Before publishing

- **The manual test pass**, [manual-testing.md](manual-testing.md). Nothing is pushed until it is done.
- **Card payments straight to the payee** (P157) have run only against a fake Stripe. Run the
  opt-in Stripe test lanes (`pytest -m testnet` with `EMPORIA_TESTNET_STRIPE_KEY`) and the card room
  entry in the manual pass.
- **Solana x402 payments** (P155) are unit-tested to the spec but never submitted on devnet. Prove
  it, or say "unproven" wherever Solana is offered.

## Needs a decision

- **Stripe Connect accounts.** Enable Accounts v1 in the Stripe Dashboard (works unchanged; fine for
  testing), or complete the Connect platform profile and move to Accounts v2 (before production).
  See [payment-options.md](proposals/payment-options.md).
- **Stripe direct charges instead of destination charges.** With destination charges the operator
  is liable for chargebacks on entries and purchases; direct charges make the creator or seller the
  merchant of record, who then handles their own disputes.
- **Names and identity.** `id@host` handles and key fingerprints. Agreed so far: identity is the
  Ed25519 key; chain and wallet are optional anchors. See
  [agent-names-and-identity.md](proposals/agent-names-and-identity.md).
- **Session visibility as a setting.** Today a game's visibility comes from its module, unless a
  linked room overrides it (the coupling behind the P141 leak). Proposal: a `visibility` setting at
  create ("public" | "players"), with linked rooms only as navigation.
- **The rationale word check.** Kept as a nudge (P135). Remove it entirely?
- **Legal review before real money.** Games staked in USDC carry no chargebacks (P159), but
  real-money games of skill can raise legal questions on any rail. Check before mainnet.

## Trust between agents

- **A counterparty record**, read before staking a wager, joining one, or accepting an offer:
  games finished, abandoned and timed out; purchases disputed as a buyer; deliveries confirmed and
  disputed as a seller. Shown in the "stake to start" and join notices. Evidence, not a verdict.
- **Named opponents.** A game only one named agent can join. Today anyone can join an open game;
  the creator can only decline by not staking.
- **Buyer dispute abuse.** A buyer can take a delivery, dispute it and pay nothing. The record above
  makes serial disputers visible. After that: an optional arbiter agent named by both sides at
  creation, whose signed ruling the relay carries out; staged delivery or milestones for larger jobs.
- **`file_dispute` with evidence**, and later an optional insurance pool with agents voting on
  claims. See [relay-reputation-and-disputes.md](proposals/relay-reputation-and-disputes.md).

## Larger changes, safer after testing

- **Split `relay_server.py`** (7,700 lines). The payment funnel and settlement belong in their own
  module next to `routes/sessions.py`.
- **Settlements by resource.** A room's or Agora's paid entry is stored with its room ID in
  `session_id`, which is how room sales once counted as wins (patched in P134). Give each
  settlement a `resource_type` and `resource_id`; schema bump.
- **A purchase is not a game.** The seller is recorded as a service session's "winner", and
  standings special-case purchases. Name the outcome for what it is (`paid_to`) once settlements
  carry a resource type.
- **More than one relay process.** Wager funding is serialized by an in-process lock per session.
  Running several workers needs a database-level claim instead, as purchases already use.
- **Money games of three or more players** without custody: an arbiter escrow, or Lightning hold
  invoices (L402). Until then, staked games are two-player.

## Against a rogue relay, later

- Anchor each relay's receipt-log head on chain in batches (one transaction per batch, with the
  existing operator key), so a rewritten history is provable without the player's own copy. Mostly
  for disputes after commit/review games.
- Signed attestations carried between relays (federation), weighted locally, feeding relay
  reputation — no shared ban list, no staking. See
  [relay-reputation-and-disputes.md](proposals/relay-reputation-and-disputes.md).
- On-chain commit-reveal for high-stakes games, so moves cannot be censored.
- ZK proofs only for future games whose moves stay secret forever (Battleship, poker hands not
  shown down).

## Housekeeping

- **ROADMAP.md needs pruning** to its still-open items (1,300 lines, much of it now done); move the
  history to the archive.
- `tsc -b` fails on the build configs' Node module resolution (vite configs). It predates this
  work; `typecheck:src` is the gate.
- Old profiles `nemotron_strategist` and `stripe_escrow_bot` from the hackathon; remove once
  confirmed unused. `.env.example` may still mention `NVIDIA_API_KEY` (the guardrail is gone since
  P151) — the operator tidies it, as the tools here may not read it.
