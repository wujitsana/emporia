# Handoff — `post-hackathon`, 2026-10-28

For whoever picks this branch up next (a cloud coding agent, or a person). It says where the work
stands, what was tested by hand, what is left before `main`, and what can and cannot be done
without the original machine.

## Where things are

- **Branch:** `post-hackathon`, ~530 commits ahead of `main`. Not merged. Aim: merge by the end of
  the week of 10-26 so others can test.
- **Tests:** `pytest` (≈1,700, ~3½ min) and the dashboard's `cd dashboard && npx vitest run` (≈180)
  all pass. `npx tsc -b` reports 35 errors, all pre-existing (srcl's types, `vite.config.ts`).
  Don't add to them.
- **Change log:** each change is a numbered `P…` commit; `docs/security-log.md` records the
  security-relevant ones. P200–P239 were this last stretch.
- **Plans:** `ROADMAP.md` (what's next, open decisions), `docs/manual-testing.md` (the hand-test
  plan), `docs/agent-security-setup.md` (keys, roles, the signer, environments).

## What this stretch built (P200–P239)

- **Secrets:**
  - The relay reads its secrets from files (`EMPORIA_SECRETS_DIR`), never compose environment.
  - Profile secrets live only in the profile's `.env`, declared `password: true` so Hermes strips
    them from agent-run commands.
  - `emporia agent secure` moves stray secrets out of `config.yaml`.
- **Live-money guard:** a relay refuses real money when run from a Hermes profile. Hosting for
  other machines is `emporia relay host`, which a person runs.
- **The signer, phase 1 (P203–P209):** wallet keys in a separate process. It is optional on
  testnet and required for mainnet. Commands: `emporia signer add|remove|host|…`. See
  agent-security-setup.md § The signer.
- **The `emporia` command:**
  - Arrow-key menus with Esc to go back, and wizards that go back a step.
  - Headings, and a calmer `doctor` (`--all` for every check).
  - New verbs:
    - `relay restart`;
    - `relay remove NAME` (an operator retires a dead agent);
    - `agent refresh NAME|all` (a fresh plugin copy and the dashboard tab);
    - `agent inbox-clear NAME|all` (a one-off backlog clear).
- **Agents:**
  - `invite_to_room` (MCP tool count is now 70).
  - DM notices ask for the last 5 messages, not the whole thread.
  - A failed chess move says when another of the agent's own conversations moved first (P226).
  - Chess `max_moves` (default 40 each; reaching it is a draw).
  - At most `EMPORIA_THREAD_LIMIT` (default 20) agent messages per conversation per hour reach an
    agent (P230).
  - An event the bridge delivers to the agent is marked read (P233).
- **Dashboard (the Emporia tab in the Hermes dashboard):**
  - It follows the Hermes profile it is open in. Its picker switches Hermes's own profile (P234).
  - A profile that is not an agent sees only a note.
  - It makes **no writes at all**: human actions in the dashboard are deferred (P234).
  - Times carry dates (P228).
  - Rooms and live games fall back to fetching every 5 s when the websocket is refused (P236).

## Tested by hand on 10-27/28 (the user's machine: Hermes in Docker, local testnet relay)

Passed:
- Setup step 0: plugins refreshed, secrets secured, doctor clean apart from optional warnings.
- DMs both ways, encrypted, read on the dashboard.
- Room invite end to end: alpha invites, beta joins on its own. Room chat stopped by itself.
- A free chess game ran. The "auto-player bug" the agents reported was each agent racing its own
  game conversation; that was fixed (P226) and is not a relay bug.
- Dashboard tab per profile, picker, unread count, test-room messages (after P236).
- Safety: an agent refuses to send its recovery phrase. `emporia relay host` is refused to an
  agent by the key guard.

Also passed (10-28, after P236):
- Encrypted room: created, invite sent, beta joined on its own. The dashboard shows each message
  as signed and 🔒.
- Kick: permanent, and alpha explained that rejoining is impossible.
- Chess from a set position with `max_moves: 3`: it ended as a draw, `termination: move_limit`.

Found on 10-28, and what was done:
- **Agents start work on their own.** A "hello" in secret-room led alpha and beta to agree to a
  chess game. The user's call: agents decide for themselves when dealing with each other. The
  guide says so, within the operator's limits: payment caps and approval, the open-games cap,
  chess `max_moves`, the per-conversation limit. Agents should keep what they take on
  proportionate.
- **The move race again.** The operator told alpha's chat to "play it out", so the chat and the
  game's conversation both moved. Alpha's chat also polled `get_session` in a loop, and the model
  emitted junk lines while waiting.
  - Fixed in the skill: the game's conversation plays even then, and agents don't poll in a loop.
- **Encrypted rooms now open on the dashboard** for a member, through the console's
  `/room/<agent>/open` (P238), as DMs do.

Also passed (10-28):
- **The dashboard picker** switches Hermes's own profile, and the tab follows.
- **The conversation limit**, with `EMPORIA_THREAD_LIMIT=3` on beta:
  - alpha sent 4 DMs; beta was shown 3, and the 4th became the "exchange is over for now" note;
  - nothing more reached beta;
  - beta still replied once, from the same turn as the batch.

## Work queue, in priority order

For the cloud agent. Work top-down, one numbered `P…` commit (or a few) per item, tests with each.
Push to `post-hackathon` only when the user says so. Items marked **(machine)** need the user's
Hermes setup; leave those for a person.

### 1. Agent-signed listings — a chain of custody across relays (before the merge)

**Why.** Today an agent creates a listing over an authenticated request, and no signature from
the agent is stored. When a peer pulls listings, the origin relay signs the batch with *its own*
key, embedding each seller's public key (P184). That proves "relay R says agent A, key K, listed
this", not that A said it: on listings, a relay is trusted to speak for its agents.
- Laundering is blocked today only because listings travel one hop: a relay serves only listings
  created on it (`origin_relay = RELAY_BASE_URL` in `federated_listings_out`).
- The fix makes each listing verifiable end to end, agent → origin relay → receiver, without
  trusting any relay in between. It is the claim the paper (item 3) rests on, and someone will ask.

**Build:**
1. **`wire.py`:** `signed_listing_payload(agent_id, origin_relay, listing_nonce, title, description,
   listing_type, payment_mode, price_usd, module_type, public_metadata, expires_at)`, following
   `signed_action_payload` and `signed_room_message_payload`.
   - `listing_nonce` is chosen by the client (`wire.new_message_nonce()`), so the signature
     can be made before the relay assigns `listing_id`. Make the relay derive `listing_id`
     from it, or store both.
   - Sign only the **public** metadata. `public_listing()` strips `_PRIVATE_LISTING_METADATA`,
     and a receiver must be able to rebuild exactly what was signed.
   - `expires_at` is absolute, chosen by the client, and bounded by the relay (today the relay
     computes it from `expires_in_hours`).
2. **`POST /listings`** (`routes/listings.py`, `CreateListingRequest`): require `signature` and
   `listing_nonce`, and verify against the caller's registered key before inserting.
   - No backward compatibility is owed: an unsigned create is refused, with a version bump.
   - Store the signature and the signed fields: schema migration, new columns, bump
     `SCHEMA_VERSION`.
   - Check every *other* path that writes `listings` (bounties via `post_bounty`, events,
     anything else `INSERT INTO listings`). Each either gets an agent signature too, or is
     marked relay-authored and excluded from federation. Don't leave a silent unsigned path.
3. **Callers sign automatically:** `mcp_server.create_listing` and `agent_sdk.create_listing`
   (and the bounty and event tools, if they create listings), as `submit_action` does.
4. **Federation out** (`federated_listings_out`): include `signature`, `listing_nonce`, the signed
   fields as signed, `agent_public_key_hex` (already there) and the agent's `stable_id`.
5. **Federation in** (the importer in `relay_server.py` around the P83/P87 ownership checks).
   Per listing, refuse it unless:
   - the agent's signature verifies against `agent_public_key_hex`; and
   - `stable_id_for(agent_public_key_hex) == stable_id`. The `stable_id` is the SHA3 of the
     genesis key (P47), so the key vouches for itself, and a relay can't substitute a key it
     made up.

   Count refusals in the sync record that `GET /federation/peers` shows. Keep the batch
   signature and the peer-key pin: they prove which relay sent it, a separate fact.
   - **Key rotation:** the agent's current key won't hash to its `stable_id`. Rotation isn't
     implemented yet (ROADMAP § Identity keys), so refuse, with a clear note, and leave the
     rotation-chain check for when rotation exists.
6. **The same for the lobby:** `federated_open_sessions` (`/federation/v1/open-sessions`) is
   relay-signed only, so open games need the same treatment. Do it in the same item, or
   immediately after.
7. **Clients check too:** `scan_relays`, and anything in the MCP or SDK that reads another relay's
   listings, verifies the agent signature itself rather than trusting the relay that served it.
8. **Tests:**
   - a forged listing (right relay signature, wrong agent signature) is refused on import;
   - a listing whose key doesn't hash to its `stable_id` is refused;
   - a relay re-serving another relay's listing can't alter a field without the check failing;
   - unsigned creates are refused;
   - MCP and SDK creates verify end to end through a real relay round trip.
9. **Docs:**
   - `docs/protocol.md` §4 (the signed-batch table, plus a per-listing signature row);
   - `SECURITY.md` and `docs/security-log.md` (the finding and the fix);
   - README "How it fits together", if it mentions federation;
   - the `emporia-dev` skill's route and tool notes.
10. **Then, optionally:** with listings verifiable end to end, relays could re-serve listings
    pulled from peers (multi-hop discovery) without trusting the middle hop. That's a separate
    decision for the user; don't build it unasked.

### 2. Remaining hand tests before the merge **(machine)**

`docs/manual-testing.md` §1b (setup and safety, the signer in practice mode) and the rest of §2.
Also item 1 above, on the real setup: two agents' listings visible, and a forged one refused.

### 3. The paper — after item 1 lands

A short technical paper, `docs/paper/` (Markdown is fine), on what is unusual here, each claim
pointing to its code and tests:
- **Chain of custody for listings across independently run relays** (item 1). Why one hop was
  the safe default, and what end-to-end signatures allow.
- **Self-certifying agent identity:** `stable_id` = SHA3 of the genesis key, and handles that
  can be renamed (P47).
- **Non-custodial payments across chains:** x402 direct and escrow holds (Base, Solana), Tempo,
  Stripe destination charges. The relay holds nobody's money; an escrow hold is reclaimable
  without the relay.
- **Privacy from the operator:** end-to-end encrypted DMs and rooms, sealed DMs between relays
  with consent, hash chains any client can recompute against a pinned head.
- **Fairness without anti-cheat:** money only on games an engine can't simply win (P132–P136).
- **Agents acting for people:**
  - roles;
  - keys kept from the agent's own tools;
  - caps, and approval through the agent's own app;
  - what only a person may do;
  - agents deciding among themselves within limits enforced in code.
- **Honest limits:**
  - metadata is visible to relays;
  - one hop before item 1;
  - no external review, no mainnet, no load test.

No competitor or other-project names (standing rule). Draft it; the user reviews before anything
is published.

### 4. Smaller items, any order

- **Rooms can't be edited or deleted**, and a "private + paid" room is listed publicly. **Needs
  the user's decision** (ROADMAP § Needs a decision); propose options, don't build.
- **Hermes issues to draft** (text only; the user posts them): the four behaviours under "Hermes
  behaviours this code works around" below.
- **Agents write bug reports; a person files them** (ROADMAP § Before publishing). Design it
  first: where the report goes, and what it contains.
- **README status and `ROADMAP.md` § Before publishing:** update as items close.

### Later, by the user's ordering

- **Signer phase 2.** Waits for phase 1's hand test, item 2.
- **Revisit the conversation and chess limits** when trying agent comms on purpose.
- **Desktop app plugin:** deferred.

## Left before merging to `main`

1. Run the rest of `docs/manual-testing.md` that one machine can do: §1b setup and safety, §2
   rooms, the signer in practice mode (§1b "Signer").
2. Things needing extra setup, to do **after** merging:
   - two relays (federation, visiting, move/backup) — run as two relays on one machine first;
   - `relay host` with a domain and https;
   - the relay as a system service;
   - Stripe Connect payouts (enable Accounts v1 in the Stripe test dashboard);
   - x402 paid rooms (testnet USDC in the agents' wallets).
3. Update README's status and `ROADMAP.md` § Before publishing as items close.

## What can be done without the original machine

- **Can:**
  - code, unit tests and the dashboard's tests;
  - running a relay locally (`emporia relay start`) and driving it with the SDK or `pytest`;
  - docs;
  - review.
- **Can't:**
  - anything that needs Hermes: the gateway, the platform plugin's live delivery, the dashboard
    tab inside Hermes, agents' model turns;
  - the user's agents' keys;
  - the user's Stripe test key.

  Don't fake these; leave them for a person, and say so.

## Hermes behaviours this code works around

Upstream; worth a Hermes issue each:
- A plugin's **websockets are refused in any profile but the launch one** ("does not carry
  websockets yet"). The tab polls instead (P236), and its live event feed is off in agent profiles.
- `/api/plugins/*` requests carry **no `?profile=`**. The tab reads its profile from its script
  tag (`data-hermes-profile`) or the page URL instead (P227, P234).
- `hermes -p NAME plugins enable emporia` **resolves to the platform plugin** (`platforms/emporia`)
  and leaves the dashboard tab off. The installer writes the entry itself (P225).
- **No platform option for "background" sessions.** Agent-to-agent conversations crowd the
  session list; the user hides them by hand. Proposed upstream: a `PlatformEntry` field such as
  `sessions_hidden`, applied when the gateway creates a session for that platform.

## Next work, already decided

- **Signer phase 2:** the identity key behind the signer, with approval for moves, rotations,
  backups and deletes. It waits for phase 1's hand test. See ROADMAP.md § Agent wallets.
- **Agents write bug reports; a person files them.** See ROADMAP.md § Before publishing.
- **Revisit the conversation and chess limits** when trying agent comms on purpose. See
  ROADMAP.md § Needs a decision.
- **Desktop app plugin:** deferred. Only a complete native dashboard with updates, or nothing.

## Standing rules (from the user)

- **Pushing:** nothing goes to `main` until a person has tested by hand; the automated tests are
  not enough. Feature branches may be pushed.
- **Compatibility:** no backward compatibility is owed. Make a break loud (bump the version)
  rather than shimming it.
- **Names:**
  - No competitor or other-project names in code, docstrings or repo docs; record the property,
    not the source.
  - Don't hard-code `/opt/data`; use `HERMES_HOME`. Other setups mount Hermes's data elsewhere,
    or run Hermes on the host.
- **Secrets and money:**
  - Never print or commit secrets.
  - Testnet only.
  - Blockchain transactions need the user's say-so.
  - Ask before anything likely to cost more than $3 in model or API spend.
- **What the user decides:** irreversible actions (deleting keys, removing agents) are the
  person's to run.
- **Wording:** plain words in the CLI and docs. The user chose "add/remove" over "adopt/release",
  and "remove" over "retire".
