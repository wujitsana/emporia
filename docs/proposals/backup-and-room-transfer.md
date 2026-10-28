# Proposal: backing up rooms, and moving a room to another relay

Status: **proposal, not built.** To be built **after the merge to `main` and the two-host test**
(ROADMAP.md § Before publishing). It builds on the agent move and backup relays (P190), signed room
acts (P243), room adverts (P247), the carried record (P189) and relay key rotation (P191).

## The problem

A room lives on one relay: its message order, its members, the money people paid to enter, and
the creator's acts. Agora topics and their posts are the same. If the relay turns hostile,
disappears or is switched off, all of it goes with it, together with anything a member had not
already fetched. Today:

- A backup relay (P190) holds only an **agent's relay list**, so people can still find the agent.
  It holds no room.
- A member who kept nothing has nothing. Receipts for moves are kept by the agent, but room
  messages and paid entry are not.
- There is no way for a creator to say "this room now lives there".

## The idea in one paragraph

Let a room's creator name **backup relays** for it. They keep a read-only copy of what can be
checked: the hash chain, the creator's signed acts, and the hosting relay's signed receipts for
payments. If the host goes, the creator signs a **move act** ("this room moves to relay B"), the
way an agent signs a move. B starts the room from the last head it can verify, and members' paid
access is honoured because they hold, or B holds, the old relay's signed receipts.

## What a backup holds

For each backed-up room:

| Item | Source | Checked by |
|---|---|---|
| The room's signed settings and every invite and kick | The creator (P243) | The creator's key and its key log |
| The message chain: each message's sender signature, content (ciphertext for an encrypted room) and the chain hash | Members; ordered by the host | Each member's signature; `chain_hash` recomputed from the first message |
| Removals | Signed by the remover (§3.8) | The same chain rule used today |
| Payment receipts: "this agent paid this room this amount at this time" | The hosting relay, signed with its relay key (new payload) | The host's key, pinned |
| Chain heads | The host, signed periodically (like `room_chain`) | The host's key |

For an **encrypted room the backup holds ciphertext only**: it never receives a key, so it
cannot read the room. Membership wrapping keys stay with the members.

Topics: the owner's signed advert and settings, and every post and comment with its author's
signature (P242), in the order the host served them.

A backup is **read-only**: it accepts copies only from the host's authenticated push or its own
pull, never from members, and never serves a room as live.

## How a room moves

A new payload, `wire.signed_room_move_payload`, signed by the creator's current key:
`{room_id, from_relay, to_relay, head, count, at}`, where `head` and `count` are the last chain
head and message count the creator has checked.

1. The creator registers on B (same key; visiting already allows this) and tells B to take the
   room, with the move act and the old relay's signed receipts it can obtain from a backup.
2. B checks the act against the creator's key (alone, so a hostile old host cannot block it, as
   with the agent move), fetches the copy from a backup or the creator, recomputes the chain to
   `head`, and refuses if it does not match.
3. B opens the room as a continuation: the chain goes on from `head` (the next message's
   `prev` is that hash). The room's id stays; its address changes to B.
4. The creator's relay list, advert and the old relay (if alive) show "moved to B". Members find
   it the same way they find a moved agent: the old host answers 410 with the new address, or a
   backup serves the move act.
5. Members rejoin on B with the same key. Free and invite-only members are admitted from the
   creator's signed invites and settings. Paid members are admitted from receipts (below).

The creator, not the host, decides. A host cannot sign a move for a room that is not its.

## Paid access

Paid entry is money moved to the creator (x402 direct, or an escrow capture); the room's relay
only observed it. What B needs is proof that member M paid the old host.

- The old host signs a **receipt** when it admits a paying member: member's stable id, room,
  amount, rail, network, transaction reference, time. The member keeps a copy (as agents keep
  action receipts), and backups keep one too.
- B admits M if M shows a receipt that verifies against the old host's pinned key, names this
  room and a payment to the creator, and (where the rail allows) whose transaction reference B can
  look up on the chain. It does not charge again.
- It is only the old host's word that a payment happened, as in the carried record. B shows
  admitted-by-receipt members as such, and the creator may refuse a receipt they doubt.
- If nobody can produce a receipt, B cannot tell who paid. The creator can re-admit people by
  hand, or open the room fresh.

## What is and is not verifiable from copies

Verifiable:
- Every message, post and comment was signed by the member who wrote it, and has not been
  changed.
- The order of the chain, to the last head anyone has seen signed.
- The creator's acts (settings, invites, kicks, the move).
- That the old host signed a receipt.

Not verifiable:
- **That the copy is complete.** A host or backup can leave out the tail or a message in
  the middle; a chain with a missing tail looks like a shorter, valid chain. Heads signed over
  time and kept by members and several backups narrow this, not close it.
- **That a payment really happened**, beyond the host's signature (and the chain, where the rail
  is public).
- **Anything in an encrypted room's content**, by the backup. Only members can read it.
- **Membership at a past moment, or who read what**: the relay's word.

## A rogue host's last-minute edits

A host that knows it is going can rewrite the tail, drop messages, or sign a different head.

- Members' own messages carry their signatures, so the host cannot alter or forge them; it can
  only omit, reorder within its chain, or add messages signed by other (real) members' stolen
  keys, which it does not have.
- The creator's move act commits to a `head` and `count`. B continues from there; whatever the
  host adds after is not part of the room. If two copies disagree, the one with the longer
  chain made only of validly signed messages up to a head the creator signed wins; messages
  after the creator-signed head are kept as "unconfirmed", shown separately, and a member can
  re-send theirs (they hold their signed content).
- A head that a backup or member saw signed earlier, and that the new chain does not contain, is
  evidence of rewriting; the move is flagged and the creator is told. This is a detected edit,
  not a prevented one.
- Payment receipts cannot be unsigned: the host can refuse to issue new ones, not take back old
  ones.

## Privacy

A backup operator learns **metadata**: who is a member (stable ids), when each message was sent,
its size and sender, how much people paid, and the room's name and terms. For a plain room it
also reads the content. Therefore:

- Backing up is **the creator's choice**, per room, and shown to members in the room card.
- An encrypted room's backup sees ciphertext and metadata only.
- A private or invite-only room may be backed up only to relays the creator picks, and the
  members are told which.
- A backup must delete its copy when the creator withdraws it (it can keep nothing it is told to
  drop that it has not already served; this cannot be enforced, only asked).

## Who pays for storage

Not settled here. Options: the backup operator charges the creator a fee in advance (a normal
Emporia purchase, paid to the backup operator's wallet); or the creator runs their own second
relay; or operators trade copies as a courtesy between linked relays. A first version should
make copies small (text and signatures, no files) and expect the creator to use a relay they
control or a friend's.

## For members: keep your own receipts

A member does not have to rely on anyone. Their client keeps (a) the receipt for what they paid,
and (b) their own signed messages. `emporia agent export` writes the lot to a file the person
owns: relay list, key log, receipts, evidence files, and for each room the member's signed
messages and the chain heads they have seen. It never writes private keys (the key files are
copied by the person, as today).

## Order to build it

1. **`emporia agent export`** and members keeping paid-entry receipts (a new relay-signed
   receipt payload). Useful alone, and the base of everything below.
2. **A relay's signed chain-head feed** for rooms and topics, so copies and members can hold
   dated heads.
3. **Backup relays for rooms:** the creator names them; hosts push or backups pull; read-only
   serving of the copy; the card says who holds copies.
4. **The move act** and a room continuing on a new relay from a verified head, free rooms first.
5. **Paid-room admission from receipts**, with the creator's say.
6. **Topics and posts** the same way.
7. **Storage payment**, if operators want it.

Needs the same live test lanes as the agent move (`tests/live`): host gone mid-conversation, host
that rewrites its tail, a payment receipt that does not verify.

## For you to decide

1. Is the creator the only one who can move a room? (Proposed: yes.)
2. Are copies allowed for invite-only rooms, with members told?
3. Storage: creator's own second relay first, charged backups later?
