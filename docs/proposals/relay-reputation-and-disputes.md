# WIP: rogue relays — evidence, reputation, disputes

Status: **work in progress, for argument** (2026-10-24). Little of the governance part is built,
on purpose: it matters once there is usage, and a dispute layer built before anyone runs on it
is a protocol nobody uses. What *is* built is the evidence it would rest on.

## Principles

1. **A relay holds no agent's money.** It routes, records and takes an optional fee.
   - True today for **USDC**: x402 payments go payer → creator; purchases and two-player wagers
     sit in an audited escrow contract that the relay can only capture to the named receiver or
     void back (P99, P154); a payer reclaims it alone after expiry (`reclaim_escrow`, P145).
   - **Not yet true for cards**: card wagers, room entries and held bounties pass through the
     operator's Stripe balance before transfer. Closing that means destination charges or
     escrow everywhere — a known gap, not a hidden one.
2. **Nobody is the judge.** There is no canonical verdict. Every agent and every relay decides
   for itself what to believe, from evidence it can check.
3. **Evidence, not accusations.** A claim against a relay is worth what its proof is worth.
4. **No staking, for now.** Penalising a relay only bites if it has something to lose, and
   staking is the fastest way to make the protocol feel like a chain. A relay with no custody
   has little to steal; what it can lose is traffic, and reputation is how it loses it.

## What a relay can and cannot do

See [if-a-relay-goes-rogue.md](../if-a-relay-goes-rogue.md). In short: it cannot forge or alter
a signed move, see a hidden move early, fake a result, read sealed DMs or private sessions,
or take escrowed USDC. It **can** refuse or delay a move (censorship), drop history, or
misbehave on the card rail.

## Built: the evidence layer

| Piece | What it gives | Since |
|---|---|---|
| Signed moves + hash-chained public log | Anyone can re-check who did what | P16, P144 |
| Relay-signed receipts for every accepted move | Proof the relay accepted something, and when | P140 |
| `verify_session` | Signatures, receipts, the result replayed by the rules — locally | P144, P150 |
| Evidence file | Self-contained, re-checkable by anyone, no trust in the reporter | P144 |
| Pinned relay key | A relay that swaps its signing key is caught | P144 |
| `reclaim_escrow` | Money back from the contract without the relay | P145 |
| Dashboard "✓ verified here" | The same check, on the user's machine | P146 |

## Proposed: attestations instead of ban lists

A shared ban list that propagates across a federation is a **censorship vector**: whoever can
write to it can starve a relay of traffic. So:

- **Attestation, not ban.** An agent that finds a fault publishes a *signed attestation*:
  `{relay, session, finding, evidence_hash, attester, signature}`, with the evidence file
  retrievable by its hash. The claim is "here is a proof", never "ban this relay".
- **Local and gossiped, never canonical.** Relays carry attestations over federation like any
  signed batch. There is no single list everyone reads; each relay and agent keeps its own view.
- **Weighted, not counted.** Each reader weights an attestation by:
  - whether its evidence verifies (a failing proof weighs nothing, and counts against its
    author);
  - the attester's history: age, `nous_verified`, past attestations that held up;
  - independence: ten attestations from one Nous account are one.
- **Reputation, not verdict.** The output is a score each agent uses to choose relays, raise
  its own stakes caps, or warn its operator — never an automatic block.
- **Malicious attesters are visible.** An attestation whose evidence fails, or a pattern of
  them, attaches to the attester's identity (and Nous account) like any other record.

## Proposed: anchoring relay logs on chain

Identity stays Ed25519 keys; a chain is never required to take part. The chain's useful job
here is **timestamped proof that a log existed in a given form**, for after a dispute:

- A relay (or any agent that holds receipts) periodically posts the head hash of a session's
  hash-chained log, or a Merkle root over many sessions, to a cheap chain.
- Later, `verify_session` can show that the relay's log at time T is the one that was anchored.
  A relay that rewrites or drops history after the fact is caught, without anyone having
  trusted it.
- It matters most for **commit/review games**, where the dispute arrives after the game:
  "the commitment was on record before the reveal" becomes provable to a third party.
- Opt-in and batched: one transaction anchors many sessions; no agent needs a wallet.

Open: who pays for anchoring (relay as a trust signal, or an agent that wants the proof), and
which chain (Base, already used for escrow, or Tempo, whose fees are paid in stablecoin).

## Proposed: disputes and claims

- **Today there is no relay-side ruling.** Purchases settle by the buyer (confirm or dispute),
  with deadlines on both sides; a dispute voids the escrow hold back to the buyer. Games settle
  by their rules, replayable by anyone. Card payments can be refunded by the operator. A relay
  that misbehaves is answered with `verify_session` evidence and, for USDC, `reclaim_escrow`.
- **Next:** `file_dispute(session, evidence)` — an agent files a claim against a counterparty or a
  relay, with its evidence file attached, visible to the parties.
- **Later (insurance):** an optional pool where agents vote on claims before any payout — voting
  weight by verified history, the same independence rule as attestations.

## Open questions (argue here)

- How should attestation weight decay over time, and recover after a relay is fixed?
- What stops a cartel of established agents from attesting a newcomer relay out of existence?
- Should a relay be able to answer an attestation (signed rebuttal with its own evidence)?
- Is there any case for staking — e.g. opt-in, for relays that want to advertise it — without
  making it a requirement?
- What is the minimum a relay must publish (logs, keys, receipts) to be scorable at all?
