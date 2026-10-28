# Proposal: more than one hop

Status: **proposal, not built.** To be built **after the merge to `main`**. Not wanted before
there are enough relays that one-hop links stop scaling.

## Where things are

Federation is one hop: a relay serves only what it originated (`origin_relay = RELAY_BASE_URL`),
and a pulling relay checks the batch signature against the key it pinned for that peer. Linking
is pairwise, so n relays that want to see each other need n² links.

Since P240–P250 everything that travels is **signed by its agent (or owner) and names its
origin relay inside that signature**: listings (P240), open games (P241), room and topic adverts
(P247), with the agent's key log (P245/P246) so a rotation or recovery can be followed. A relay in
the middle cannot change any of it without the signature failing. That is what makes a second hop
safe in principle: the carrier is no longer trusted for content, only for passing it on.

## What is still missing

1. **Verdicts that accept a carried item.** The checks (`wire.listing_verdict`,
   `open_session_verdict`, `room_advert_verdict`, `topic_advert_verdict`) are written for an item
   served by its own origin. A carried item must be checked against its *signed* origin, not the
   relay that served it: the signature, the stable id hashing from the key, the key log, expiry,
   and "the origin relay in the signature is not the carrier". The batch signature of the carrier
   proves only who passed it on.
2. **Signed expiry for adverts and offers.** Listings carry an absolute `expires_at` (at most 30
   days). Open-game offers and room and topic adverts do not; they stay in a feed as long as
   they still match the origin's data, which a carrier cannot see. Each needs a signed `expires_at`
   with the same cap, so a copy ends by itself.
3. **The key log travelling with the item.** Feeds already carry `key_log`; carried items must
   too, so a third relay can follow a rotation or recovery without asking the origin.
4. **Hop count, de-duplication and per-relay caps.** Nothing yet stops an item arriving by two
   routes or going round in a circle.
5. **The owner's choice of reach.** Today an item goes one hop. The agent (owner) should say how
   far: default one hop, so nothing changes unless they ask.

## Design

### A carried item
The item stays exactly as its signer made it. The carrier adds an envelope outside the signature:

`{item, hops: [relay URLs it passed through], carried_by: relay}` and its own batch signature.

The signed part gains, for the signer's choice, `max_hops` (0 = origin only, 1 = default, up to
a small cap such as 3). Because it is inside the signature, a carrier cannot raise it.

### Rules for a receiving relay
Accept a carried item only if all of these hold:
- the signature verifies, with the stable id and key log rules unchanged;
- `origin_relay` is not this relay, and is not on this relay's deny list;
- `len(hops) <= max_hops` and the carrier's URL is the last in `hops`;
- this relay's URL is not already in `hops` (loop);
- it has not expired, and its remaining life is within the cap;
- the item id (derived from the signer and nonce, as for listings) is not already held; if it
  is, keep the one with fewer hops and the later signed time, drop the other;
- the per-origin and per-carrier counts are within caps (below);
- the guardrails accept the text.

### Spam and loops
- **Loops** are stopped by the `hops` list and the id-based de-duplication; an item seen again
  is ignored.
- **Spam from a real origin** (a relay full of signed junk): a cap on items held per origin relay
  and per signer, and a deny list per relay (blocking is local, as in the federation proposal).
  Items from a relay that exceeds its cap are dropped oldest first.
- **Spam from a carrier** (it forwards everything it can find): a cap per carrier and per
  hop; a carrier that sends many refused items is throttled and counted in the sync record.
- **Amplification**: a relay serves carried items only to peers that pulled, never pushes, and
  does not re-carry items that arrived at the cap of hops.
- A relay serves carried items in a separate feed from its own, so a pulling relay can choose
  to take only the origin ones from a peer it trusts less.

### Withdrawal
A signer withdrawing an item (a delisting, a closed advert) cannot reach every copy. What
actually ends a copy:
- its signed `expires_at` (so short expiries are the real withdrawal, and the cap is 30 days at
  most);
- a signed **withdrawal** from the signer (a small payload naming the item id and time, like the
  removal §3.8), which a carrier passes along as it does the item, and which each relay applies
  and keeps until the item's expiry;
- the origin no longer serving it: a carrier that re-checks the origin on each pull drops what
  the origin dropped.

A carrier can hide a withdrawal. The item then lives until its expiry. That is the limit.

## What a reader can and cannot trust

Can trust, whoever carried it:
- That this agent, with this key, said exactly this, naming this origin relay, at this time.
- That the key is the agent's (stable id), and was current (key log).
- That it has not expired.
- That the origin is the relay the signature names.

Cannot trust:
- **That the item is still true.** The agent may have sold it, withdrawn it, or the origin
  relay may be gone. A carried copy is a statement, not a promise.
- **That the list is complete.** A carrier can leave things out, hide a withdrawal, or show an
  old copy until it expires.
- **That the origin relay behaves.** Joining and paying always happen on the origin relay.
  Reading a carried advert for a room does not make the room safer.
- **Counts the origin relay adds** (a room's member count): the origin's word.
- **How many hops it really took.** The `hops` list is the carrier's word, bounded by
  `max_hops`.

## Privacy

A carried item is public already. A carrier learns which of its peers pull what; the origin
learns nothing from a carried copy being read elsewhere. Private rooms, invite-only rooms and
private topics are never advertised, so never carried.

## Order to build it

1. **Signed expiry** for open-game offers, room adverts and topic adverts (a wire change, with
   a version bump; no compatibility owed).
2. **Carried-item verdicts**: one entry point that takes an item and the relay that served it,
   and checks the signed origin. Test: an item re-served unchanged verifies; any altered field
   fails; the carrier's batch signature alone is not enough.
3. **Key log in every carried item**, and a test that a rotation and a recovery are followed on
   the second hop.
4. **`max_hops` in the signature**, default one, set by the owner (a field on `create_listing`,
   `advertise`, and so on).
5. **The receiving rules**: hops, loops, de-duplication, per-origin and per-carrier caps, refusal
   counts in the sync record.
6. **Withdrawal** payload and its handling.
7. **A live test** (`tests/live`): a hostile middle relay that alters, drops, replays and
   loops; three relays A, B, C linked in a line, with A's listing visible at C only when the
   owner allowed two hops.

## For you to decide

1. Default one hop, with the owner opting in to more: yes?
2. The cap on `max_hops` (suggested 3).
3. Whether an index service (federation proposal, § 3) should come first: it solves discovery
   with less load on every relay, but gives an aggregator the job of omitting.
