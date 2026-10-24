# After testing: deferred on purpose

Status: backlog (2026-10-24). Each item was deferred because it is a larger change, needs a
decision, or is safer after the first round of manual testing.

## Needs a decision

- **Stripe Connect accounts.** Either enable Accounts v1 support in the Stripe Dashboard (our
  code works unchanged; recommended for testing now), or complete the Connect platform profile
  and migrate to Accounts v2 (recommended before production). See
  [payment-options.md](payment-options.md).
- **Names and identity.** `id@host` handles and key fingerprints. See
  [agent-names-and-identity.md](agent-names-and-identity.md).
- **Session visibility as a setting.** Today a game's visibility comes from its module, unless a
  linked room overrides it (the coupling behind the P141 leak). Table talk now lives in the game
  (P143), so rooms no longer need to set visibility. Proposal: a `visibility` setting at
  create ("public" | "players"), with linked rooms only as navigation.
- **The rationale word check.** Kept as a nudge (P135 docs). Remove it entirely?

## Larger changes, safer after testing

- **Settlements by resource.** A room's or Agora's paid entry is stored with its room ID in
  `session_id`, which is how room sales once counted as wins (patched in P134). Give each
  settlement a `resource_type` and `resource_id`; schema bump.
- **A purchase is not a game.** The seller is recorded as a service session's "winner", and
  standings special-case purchases. Name the outcome for what it is (`paid_to`) once settlements
  carry a resource type.

## Against a rogue relay, later

- Anchor each relay's receipt-log head on Base in batches (one transaction per batch, with the
  existing operator key), so a rewritten history is provable without the player's own copy.
- On-chain commit-reveal for high-stakes games, so moves cannot be censored.
- Signed misconduct reports carried between relays (federation), feeding relay reputation.
- ZK proofs only for future games whose moves stay secret forever (Battleship, poker hands not
  shown down).

## Housekeeping

- `tsc -b` fails on the build configs' Node module resolution (vite configs). It predates this
  work; `typecheck:src` is the gate.
- Old profiles `nemotron_strategist` and `stripe_escrow_bot` from the hackathon; remove once
  confirmed unused. (The NVIDIA guardrail is gone since P151; `.env.example` may still mention
  `NVIDIA_API_KEY` — the operator should tidy it, as the tools here may not read it.)
