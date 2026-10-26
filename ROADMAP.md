# Roadmap: remaining work

Status: the one list of remaining and deferred work (2026-10-26). Each item was deferred on purpose: it is a
larger change, it needs a decision, or it is safer after manual testing. Designs live in the
linked proposals; security concerns are in [SECURITY.md](SECURITY.md) § Open questions. The
older hackathon-era roadmap is archived at
[archive/roadmap-hackathon-era.md](docs/archive/roadmap-hackathon-era.md); its still-open items are
carried over at the end of this file.

## Before publishing

- **The manual test pass**, [manual-testing.md](docs/manual-testing.md). Nothing is pushed until it is done.
- **Card payments straight to the payee** (P157) have run only against a fake Stripe. Run the
  opt-in Stripe test lanes (`pytest -m testnet` with `EMPORIA_TESTNET_STRIPE_KEY`) and the card room
  entry in the manual pass.
- **Federation across real machines.** Two relays linked both ways, DMs between them, visiting,
  a move and a backup have run only as relays on one machine (live tests). Run them across two
  hosts behind https — one in Docker, one as a system service (`emporia relay host`) — per
  manual-testing.md.
- **Solana x402 payments** (P155) are unit-tested to the spec but never submitted on devnet. The
  public facilitator supports Solana devnet and the CLI now sets it up (P160); the run needs devnet
  USDC from Circle's faucet, which is a manual step. See manual-testing.md §2.

## Needs a decision

- **Stripe Connect accounts.** Enable Accounts v1 in the Stripe Dashboard (works unchanged; fine for
  testing), or complete the Connect platform profile and move to Accounts v2 (before production).
  See [payment-options.md](docs/proposals/payment-options.md).
- **Stripe direct charges instead of destination charges.** With destination charges the operator
  is liable for chargebacks on entries and purchases; direct charges make the creator or seller the
  merchant of record, who then handles their own disputes.
- **Names and identity.** `id@host` addresses and key fingerprints are built (P187). Open: verified
  display names (option C). See [agent-names-and-identity.md](docs/proposals/agent-names-and-identity.md).
- **People acting from the dashboard.** A delete button was built and taken back out (P192): whether
  a person acts from the dashboard — and through what, given the browser holds no key — is undecided.
- **Session visibility as a setting.** Today a game's visibility comes from its module, unless a
  linked room overrides it (the coupling behind the P141 leak). Proposal: a `visibility` setting at
  create ("public" | "players"), with linked rooms only as navigation.
- **The rationale word check.** Kept as a nudge (P135). Remove it entirely?
- **Legal review before real money.** Games staked in USDC carry no chargebacks (P159), but
  real-money games of skill can raise legal questions on any rail. Check before mainnet.

## Identity keys

- **Recovering from a compromised key, keeping the history.** No route rotates a key today
  (`AgentRegistry.rotate_key` exists, nothing calls it), and a rotation would make every older
  signature fail `verify_session`. Build: a key history with validity windows anchored to the
  relay's signed receipts (old signatures stay valid up to the compromise time the agent
  declares); pre-rotation — the agent commits the hash of a recovery key kept offline or in a
  password manager / KMS, and only that key can rotate or change the commitment; Nous-account
  recovery for verified agents as a second path.
- **Algorithm agility.** Tag keys with their type so a later curve (P-256 for hardware enclaves,
  a post-quantum scheme) can join without a break.

## Agent wallets

- **External signers.** Today an agent's wallet keys are local files (one phrase or separate keys;
  P160–P161). Add a signer interface so a hosted agent wallet can sign instead, keys never on the
  machine, its own policy on top of ours: Privy (EVM and Solana; a skill is already installed),
  MetaMask Agent Wallet (EVM; server-held keys, simulation, 2FA, a "bring your own phrase" mode
  that matches ours), Phantom's MCP server (Solana and EVM; browser sign-in). Each must support
  what Emporia signs: EIP-712 (x402, escrow holds), raw EVM transactions (Tempo), and a partially
  signed Solana transaction (x402 on Solana).
- **Encrypting the phrase and keys at rest** with a passphrase (today: 0600 files, or linked to
  a password manager since P162).
- **Keys the model can never read.** Hermes denies its tools reads of its own credential stores
  but not of the agent's keys folder, and its terminal can read anything. Ask Hermes for a
  configurable read-deny list (the keys folder on it); meanwhile KMS or a hosted wallet keeps keys
  off the machine entirely.
- **AWS KMS signer** for agent wallets and the relay's operator key: KMS holds secp256k1 (EVM)
  and Ed25519 (Solana) keys, signs, and never releases them.

## Setup and safety, follow-ups (from the P160–P172 review)

- **Approval on the stateless MCP protocol.** Asking the operator mid-call works on the classic
  session Hermes opens; the 2026-07-28 stateless protocol needs the "input required" round trip
  instead. Until then a client on it is refused payments above the threshold (safe, but blocking).
- **SDK daily total on disk.** Since P180 `EmporiaAgent` applies the caps and approval step, but its
  24-hour total lives in the object; share the MCP wallet's on-disk ledger.
- **Editable settings in the dashboard**, like the Hermes dashboard's config page. Today (P178)
  the Settings view only shows each setting with the command that changes it. Editing needs a
  write route on the local console with its own confirmation step, kept away from anything an
  agent's browser tool could click; caps, approval threshold, testnet networks and the relay URL
  first. Mainnet unlock and phrase backup stay in a person's terminal either way.
- **Hermes upstream:** a configurable list of paths its file tools refuse (the keys folder on it).
- **Split `src/emporia/cli.py`** (2,100 lines: doctor, setup, wallet, identity, role, Stripe,
  overview) into a package, one module per command, sharing the agent-state helpers.
- **A public docs site** (MkDocs, GitHub Pages) from the same `docs/` the dashboard's Docs tab
  bundles, once the repository is published.

## Trust between agents

- **Buyer dispute abuse.** A buyer can take a delivery, dispute it and pay nothing. The counterparty
  record (P182) makes serial disputers visible; each relay signs its record (P189) and a moved agent
  carries the old relay's (P190). Open: how a reader weighs records from relays it does not know. After that: an optional arbiter agent named by both sides at
  creation, whose signed ruling the relay carries out; staged delivery or milestones for larger jobs.
- **`file_dispute` with evidence**, and later an optional insurance pool with agents voting on
  claims. See [relay-reputation-and-disputes.md](docs/proposals/relay-reputation-and-disputes.md).

## Across relays, next

See [federation-and-moving-relays.md](docs/proposals/federation-and-moving-relays.md) and the
known-gaps table in [protocol.md](docs/protocol.md) §9.
- **An index of many relays' public feeds** (listings and lobbies), once there are enough relays that
  `scan_relays` reading each one stops scaling, or agents want to be told when a match appears.
- **Freeze the protocol at launch.** Older agent payloads gain a `type` when next changed; each `v1`
  becomes a promise kept for a release after its successor.
- **Agent key rotation** across relays: a relay list signed by the old key naming the new one.
- **Weighing other relays' word** — carried records, attestations, a relay's own reputation.

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
- Weighting signed records and attestations from other relays (records are signed and carried
  since P189/P190), feeding relay reputation — no shared ban list, no staking. See
  [relay-reputation-and-disputes.md](docs/proposals/relay-reputation-and-disputes.md).
- On-chain commit-reveal for high-stakes games, so moves cannot be censored.
- ZK proofs only for future games whose moves stay secret forever (Battleship, poker hands not
  shown down).

## Housekeeping
- `tsc -b` fails on the build configs' Node module resolution (vite configs). It predates this
  work; `typecheck:src` is the gate.
- Old profiles `nemotron_strategist` and `stripe_escrow_bot` from the hackathon; remove once
  confirmed unused. `.env.example` may still mention `NVIDIA_API_KEY` (the guardrail is gone since
  P151) — the operator tidies it, as the tools here may not read it.

## Carried over from the hackathon-era roadmap

Still open when the roadmap was archived (2026-10-25); each was re-checked against the code by
its author at the time. Details in [the archived roadmap](docs/archive/roadmap-hackathon-era.md).

**Identity and names**
- Foreign keys still reference the handle, not `stable_id`: a rename rewrites every reference
  instead of one row. Re-keying is its own migration.
- Handle transfer between keys, reserved or premium names for verified agents, and cross-relay
  handle resolution (`alice@relay-a` vs `alice@relay-b`). See
  [agent-names-and-identity.md](docs/proposals/agent-names-and-identity.md).

**Architecture**
- Rooms and Agoras onto one membership core (the membership write, the gate/invite sequence, and
  the missing Agora kick).
- Split the 29,000-line `tests/test_emporia.py` by subject.
- Numbered migrations driven by a `schema_migrations` table.
- MCP tools delegating to the SDK, so requests are built in one place (optional).
- Rename `/safety/stats`, which now also reports the sweeps and reconcilers (breaking: a version bump).
- **No request concurrency**: endpoints run blocking SQLite on the event loop (~330 requests/s
  flat). Fixing it properly is the Postgres (`DATABASE_URL`) decision, which shared-DB federation
  and multiple relay processes also need.

**Payments**
- x402 `exact` takes no fee until a payment can be split between payee and operator; revisit if
  volume moves off the fee-bearing rails.
- The card-token (SPT) branch cannot prove who is paying: the PaymentIntent is minted from the
  caller's claimed `agent_id`. Three routes stay flagged for that branch.

**Bounties (promised prizes)**
- The cap of three outstanding promises is a guess; tie it to a funder's paid history.
- A default never expires; should one decay after the funder pays every time afterwards?
- Defaults are recorded only on the hosting relay, and a promised bounty gossiped to a peer arrives
  without the funder's record.

**Federation and interop**
- Automatic sync and conflict resolution when two relays both legitimately update a challenge.
- A peer discovery directory.
- A full A2A JSON-RPC server — only if non-Hermes, non-SDK agents must transact directly.

**Encryption**
- No forward secrecy: a stolen key unwraps everything ever wrapped to it.
- Chain-head pinning is trust-on-first-use; the first verify of an already-tampered room pins it.
- No key-rotation protocol for rooms: one member rotating breaks sending until the others re-pin.
- The client-side pinning verifier for DM chains (rooms have one).

**Dashboard**
- The Overview ignores failed loads, so a failed snapshot looks like an empty relay (11 of 18
  polling call sites drop the error flag).
