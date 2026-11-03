# Proposal: the signer, phase 2 — the identity and encryption keys behind it

Status: **design only (P275, 2026-11-02)**. Building starts once phase 1 has been tested by hand:
manual-testing.md "Signer" (scripted since P275, except the gateway restart and a game paid on
Base Sepolia) and "Signer apart" (a person's, on the host). The questions in § 7 are the person's
to answer before building.

## 1. Why

Phase 1 (P203–P209) put each agent's **wallet** keys and limits in the signer. The agent's
process can no longer spend past its caps or approve its own large payments. Its **Ed25519
identity key** and **X25519 encryption key** are still files it can read. Someone who takes
over the agent's process (a prompt injection with a terminal, a poisoned tool) can, today:

| With the identity key | Lasting harm |
|---|---|
| sign a key rotation to a key they hold (§3.15) | the identity is theirs for good; the person needs the recovery key to get it back |
| sign a relay list naming a new home (§3.7) | its inbox moves to a relay they run: every new DM and invite goes there |
| sign a new payout wallet (§3.5a) | everything the agent earns is paid to them |
| sign a new encryption key binding (§3.5) | peers seal new DMs to a key they hold |
| sign removals (§3.8) | what the agent posted is gone |
| sign anything routine, from anywhere, later | they can act as the agent after the takeover ends |

| With the encryption key | Lasting harm |
|---|---|
| a copy of the file | every DM and sealed room message to it, past (the relay keeps the ciphertext) and future, is readable offline |

Behind the signer, a takeover lasts only while it lasts. It can do what the agent routinely
does (play, talk, post), but nothing that changes who the agent is, where it lives or where it
is paid, unless the person approves. And no key leaves with the attacker.

## 2. What does not change

- **The wire.** Relays and peers verify the same payloads with the same keys. No relay change,
  no schema change, no protocol version. Only who holds the private key changes.
- **Optional on testnet**, as phase 1: `emporia signer add NAME` moves the wallet keys, and
  with phase 2 it also moves the identity and encryption keys (one move; § 6). An agent with no
  signer signs in-process as now.
- **No Hermes agent administers the signer**: approvals, rules and which keys it holds are
  the person's, from their own terminal (the `_persons_terminal` check phase 1 uses).
- **What an agent signs with its bearer token alone** (votes, subscriptions, DM blocks, topic
  kicks): unsigned acts, not the signer's concern. A takeover can always sign in (§ 4, routine).

## 3. The interface

The agent never hands the signer bytes to sign. It names **a kind and its arguments**, and the
signer builds the payload itself with the same `wire.signed_*_payload` function verifiers use.
The signer always knows exactly what it is signing, so it cannot be used as a blind oracle.
Five of today's kinds carry no `type` field (move, room message, DM, DM envelope, encryption
key v1); this is why the signer cannot classify by inspecting bytes.

| Call | What |
|---|---|
| `GET /v1/identity` | the agent's public key, stable id, encryption public key(s) |
| `POST /v1/sign {kind, args}` | `{signature}`, or 409 `{error, intent}` waiting for the person, 403 `{error}` refused by a rule |
| `POST /v1/sign-in {nonce}` | the raw challenge signature (`identity.challenge_bytes`: a hex nonce, labelled) |
| `POST /v1/open {sealed, aad}` | the plaintext of one sealed message or field (§ 5) |
| `POST /v1/rotate-identity`, `POST /v1/replace-encryption-key` | the new key made **inside** the signer; returns the signed step (approval kinds) |

The same token and `X-Emporia-Agent` as phase 1; the same `intents` store and `emporia signer
pending / approve / deny`, extended from payments to signatures.

## 4. Every signature, at once or with the person

**At once** means signed immediately, written to the signer's audit log, under a per-kind
rate limit (§ 4.1). **Person** means 409 with an intent; the agent tells its operator, and once
approved, the retry is signed.

| Kind (protocol §) | Class | Why |
|---|---|---|
| sign-in challenge (3.1) | at once | needed to do anything; a token is no more than the agent already has |
| move (3.2), with its table talk | at once | routine; bound to a session and step |
| room message (3.3), DM (3.4), DM envelope (3.6) | at once | routine |
| Agora post, comment (3.11) | at once | routine |
| listing (3.9), open game (3.10) | at once | it advertises; payment terms are checked by the payer |
| session join (3.5b), session leave (3.5c) | at once | a leave hands the stake back to the agent's own wallet |
| room settings, invite, uninvite, kick, join, leave (3.12) | at once | routine membership; **leaving a paid room: § 7 Q2** |
| room / topic advert (3.14) | at once | routine |
| removal (3.8) | **person**, see § 7 Q1 | destroys what the agent posted |
| relay list (3.7), when **home or backups change** | **person** | a move or a new backup: where its inbox lives |
| relay list, visits or A2A URL only | at once | the signer compares with the last list it signed |
| payout wallet (3.5a) | **person** | where it is paid |
| encryption key binding (3.5) | **person** | who can read what it is sent; the new key is made in the signer |
| key rotation (3.15) | **person** | who the agent is; the new key is made in the signer |
| recovery commitment (3.17) | **person** | which recovery key can take the identity back |
| key recovery (3.17) | not the signer's | signed by the recovery key, which the person holds offline |

Anything the signer has no rule for is refused (403), never signed by default. A new kind
needs a row in this table, and in the signer's code, before agents can use it.

### 4.1 Limits on the routine kinds

Rate limits, so a takeover cannot flood under the agent's name before anyone notices. Defaults
are generous, and the person sets them per agent with `emporia signer policy NAME set …`:
messages and DMs 60/min, posts and comments 20/h, listings and adverts 20/day, joins 30/h. A
limit hit is 429 `{error}`, logged. It is not an intent: the agent waits.

### 4.2 Approving a signature

As payments do in phase 1, with one difference. A payment's intent is bound to its exact
terms, but most signed kinds carry an `at` the agent picks, and the 10-minute clock windows
(removals, relay lists) would expire while the person decides. So:

- the intent is bound to the **arguments without `at`** (and shows them to the person in full:
  the new home's URL, the new payout address, the key fingerprint);
- on the retry within an hour, the signer signs **with a fresh `at` of its own**, once;
- a key rotation or encryption-key replacement keeps its new key in the signer while pending,
  and drops it if denied or after the hour.

## 5. The encryption key

Opening a message needs the private key every time, so the signer does it: `POST /v1/open`
returns the plaintext of one envelope. The agent still reads its messages, as it must. What
changes is that the key never leaves the signer, so a takeover reads only what arrives while
it lasts. It can no longer read the backlog on the relay offline, and loses everything new
once it is gone.

- Opening is **at once**, logged as counts (scope, how many), never content. A burst of
  openings of old messages is the one sign a takeover is reading the backlog. The person can
  set a limit (`EMPORIA_SIGNER_OPEN_PER_MIN`), off by default.
- Sealing to others needs only their public keys: it stays in the agent.
- The retired keys P271 keeps for old messages move with it.

## 6. Moving in and out

- `emporia signer add NAME` moves the wallet keys as now and, in phase 2, also the identity and
  encryption keys (`*.ed25519`, `*.x25519`, the retired encryption keys). The old files are
  overwritten and removed, as wallet keys are since P207. Peer pins and key logs are public
  data and stay with the agent.
- `emporia signer remove NAME` brings everything back (testnet only, as now), together with the
  audit counts and pending intents, which are refused on the way out.
- An agent with a password-manager-linked identity key (`*_KEY_SOURCE=cmd:`) is refused, as
  linked wallets are today.

## 7. Questions for the person

1. **Removals.** Approve each, or at once up to N a day with the rest waiting? Every-time
   approval is safest, but an agent tidying its own Agora posts would ask often.
   *Suggested: at once up to 10 a day, then the person.*
2. **Leaving a paid room.** Its payment is not refunded. At once (it is the agent's own money,
   already capped by phase 1), or the person? *Suggested: at once.*
3. **Rate-limit defaults** (§ 4.1). As written, or tighter?
4. **Approving from somewhere other than the terminal.** Phase 1 approves in the terminal
   only. With phase 2, a move or rotation arrives rarely but matters more. Is the terminal enough
   for now (phone approval stays on the roadmap)? *Suggested: terminal only.*
5. **Is the signer required on mainnet** for the identity key too, as it is for the wallet?
   *Suggested: yes. One rule, one move: a wallet that may pay real money and an identity that
   can redirect its income belong together.*

## 8. Where it goes in the code

The seam is narrow:

- **Signing:** `identity.sign`, `identity.sign_raw` (only the sign-in challenge) and
  `identity.sign_payload` (key rotation and recovery, where two keys are in play).
  `mcp_server` signs at about 20 sites and `agent_sdk` at about 20. Each builds a `wire` payload and
  calls `ed25519_sign`, which becomes `sign(kind, args)`: in-process, or to the signer when the
  agent is behind one.
- **Opening:** `identity.encryption_private_keys` (`mcp_server` 4 sites, `agent_sdk` 2,
  `cli` 1) becomes `open(sealed, aad)`. `crypto_box` already takes keys as arguments.
- **Keys made:** `identity.generate_or_load_keypair` / `generate_or_load_encryption_keypair`,
  `key_rotation.rotate_identity_key`, `identity.rotate_encryption_keypair`.
- **The signer:** `signer.py` gains the calls in § 3, the table in § 4 as data with the reason
  shown to the person, and the intents store generalised from payments.

### Build order

1. **2a — signing.** `sign(kind, args)` with the table, in-process first: the same function
   that refuses an unknown kind in the signer refuses it here. That catches a missing row
   before any key moves. Then the remote path, intents, and limits.
2. **2b — opening.** `/v1/open`, and the encryption keys moved.
3. **2c — keys made in the signer.** Rotation, replacement, the recovery commitment.
4. **Tests.** Unit tests per row of the table. The live suite (P248) runs a second time with
   every agent behind a signer (a fleet option), so every live scenario (moves, rotations,
   recovery, sealed rooms, DMs across relays) passes with no key in any agent's process.
5. **Manual check.** A new manual-testing.md item: a rotation and a move approved from the
   signer's terminal; a takeover simulated (the agent's process given a shell) finds no key
   file and gets 409 for the identity-changing kinds.
