# Handoff — `post-hackathon`, 2026-11-03

For whoever picks this branch up next (a cloud coding agent, or a person). It says where the work
stands, what was tested by hand, what is left before `main`, and what can and cannot be done
without the original machine.

## Plan from here (11-03, after P288) — start here

`claude/handoff-4rte3l` was fast-forwarded into `post-hackathon` on 2026-11-03 (commit `1c32360`,
through P285) at the user's approval. Work continues on `post-hackathon`, now about 19 commits
ahead of `origin` and **not pushed** (the push waits for the user). `main` waits for the manual gate.
Unit suite 2,018 passed (1 skipped); live suite 14 passed; dashboard 200 tests, typecheck, lint and
builds clean. One flaky unit test seen once: `test_encrypted_message_is_opaque_in_the_database`
(fixed agent names in a shared database).

**1. Waiting on the user:**
- Push `post-hackathon`.
- The 33 manual checks: `docs/manual-testing.md` "Before main", 8 sittings. Sitting 0 (the live
  suite) is done, 14 passed; sittings 1-8 are a person's. 63 items moved to "Covered by the
  automated suites".
- The agent-driven money run (needs credits; beta needs a USDC top-up).
- The signer apart, on the host.
- Answers to the open questions in the proposals below.

**2. New since 11-02 (P281-P287):**
- P281: transparency log off by default; clients and linked relays check it (`logcheck.py`).
- P282: review fixes.
- P283: the dashboard's own UI kit (`dashboard/src/ui/kit`) replaces `srcl`.
- P284: Hermes 0.22.6 — `EMPORIA_DAILY_TURNS` cap (a `pre_llm_call` hook); Emporia's skills copied
  into agent profiles and locked with `hermes curator lock` (`emporia agent refresh`/`update`);
  `requires_hermes >=0.22.6`; doctor warns on an exposed `api_server`.
- P285: the local relay is brought back by each agent's bridge after a container restart or crash;
  `relay stop` / `signer stop` check the pid is theirs.
- P286: relay id from the relay's key, kept across restarts and rotations (sign-ins survive
  restarts); signed `GET /relay/card`.
- P287: an agent's own analytics: `emporia stats NAME`, MCP `my_stats` (83 MCP tools),
  `/payments/records?mine=1`.
- P288: the gamify module's character sheet (progression steps 1-2) behind `EMPORIA_GAMIFY=sheets`:
  Craft (Glicko-2 per game), Trade, Trust, levels and class; `GET /agents/{id}/sheet` (signed),
  `GET /ratings`, MCP `character_sheet` (84 tools), `emporia sheet NAME`, a Craft column on the
  leaderboard; defences against agents farming it (`progression-module.md` §4.3).
- Also: dashboard lint rules back on (34 warnings fixed), a game deep-link fix
  (`GamesView.test.tsx`), `docs/manual-testing.md` restructured.

**3. Proposals for decision (design only):**
- [guilds-and-relay-identity.md](proposals/guilds-and-relay-identity.md) — decentralized agentic
  experimentation and one big marketplace of guilds; follow, friends, guilds; leagues of relays;
  signed `koinon` records underneath (naming open, §9.8); where a relay runs (practice / apart /
  remote tiers, the operator agent); co-op relays; directories, not a registry. §3 (relay
  identity) is built as P286.
- [progression-module.md](proposals/progression-module.md) — the gamify module, every part optional
  per relay (`EMPORIA_GAMIFY=sheets,seasons,quests,points,boosts,anchor`); attributes
  Craft/Trade/Trust; quests with non-money rewards; points and boosts a relay or guild may sell
  (attributes never for sale); on-chain options undecided; "worlds": a relay on its own, in one
  league, or in several.
- [analytics-module.md](proposals/analytics-module.md) — four audiences; step 1 built as P287;
  dashboard Stats view next.
- Still open: [agent-names-and-identity.md](proposals/agent-names-and-identity.md) (names per Nous
  account, hold after release, reserved list, verified mark),
  [relay-transparency-log.md](proposals/relay-transparency-log.md) step 4 and
  [robinhood-chain.md](proposals/robinhood-chain.md) (which chain, how often, spend cap; any
  transaction needs the user's say-so), range-based set reconciliation in the federation proposal
  (yes/no).

**4. Build next, in order:**
1. Fixes from the hand tests.
2. Dashboard Stats view (needs a small local endpoint to read Hermes `state.db`).
3. Count room/topic entry fees as the creator's earnings in stats.
4. The dashboard Character view; seasons and ladders (progression step 3).
5. Friends and follow (guilds proposal step 2), once naming and defaults are decided.
6. Names step 1 after its decisions; a listing purchase pinning the seller by its signed stable id
   (`payee.check` `expected_stable_id`); log leftovers (a dashboard line, salts to parties, private
   chain heads folded per state). Then by the user's choice: chain anchoring, Robinhood Chain, a
   writer thread for the database, splitting the large modules (ROADMAP § From the 11-02 audit).

**5. Before `main`:** the manual checks pass, then the two-relay, https, service, Stripe Connect
and x402 runs (§ Left before merging to `main`).

## Where things are

- **Branch:** `post-hackathon`, many commits ahead of `main` (P240–P285 came in from
  `claude/handoff-4rte3l` on 11-03), ~19 ahead of `origin`, not pushed. Not merged to `main`.
- **Tests:** `pytest` (2,018 unit tests; the full run takes about 10 minutes in a cloud container),
  the live suite `pytest -m live tests/live` (14 tests, about 5 minutes; off by default), and
  the dashboard's `cd dashboard && npx vitest run` (200). All pass; typecheck and lint are clean.
- **Change log:** each change is a numbered `P…` commit; `docs/security-log.md` records the
  security-relevant ones. P200–P239 were the stretch before the last; P240–P250 are the last.
- **Commit author:** only `wujitsana <wujitsana@gmail.com>` appears on a commit (decided 10-29).
  Set both author and committer to it (`git -c user.name=wujitsana -c user.email=wujitsana@gmail.com
  commit ...`), and add no `Co-Authored-By` line. A `Claude` committer or co-author line makes
  GitHub list extra contributors. The cost: the commits carry no "Verified" badge.
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
    - `relay remove NAME` (an operator removes a dead agent);
    - `agent refresh NAME|all` (a fresh plugin copy and the dashboard tab);
    - `agent inbox-clear NAME|all` (a one-off backlog clear).
- **Agents:**
  - `invite_to_room` (70 MCP tools then; 72 now, see below).
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

## This stretch (P240-P250)

- **Signed by the agent, checked by every reader** (`docs/protocol.md` §3, §4.2a; details in
  `docs/security-log.md`):
  - listings (P240) and open games (P241), which travel between relays;
  - Agora posts and comments (P242);
  - a room's settings, invites and kicks, with the `check_room` tool (P243);
  - room and Agora-topic adverts (P247, P249): paid rooms can be advertised by their creator, and
    relays pull each other's adverts. The `advertise` tool is the 72nd MCP tool.
- **Keys:**
  - clients pin other agents' signing keys (P244), and an encryption key's binding is checked
    against the pinned signing key (P250);
  - the key log and `emporia agent rotate-key` (P245);
  - a recovery key for a lost or stolen key, `emporia agent recovery-key` and `recover` (P246).
- **The live suite** (P248, `pytest -m live tests/live`): real relay processes over HTTP, with a
  hostile hop in between. It found four bugs, all fixed: three in P248 (rotated agents could not
  list; a move signed without the backups; a "moved" answer lost on a DM) and one in P250 (a
  first DM could be sealed to a key swapped by a hostile hop).
- **Still needs a person:** the hand tests (item 2), including all of the above on the real setup,
  and the two-host run behind https.
- **Drafted, for the person to review:** the paper (item 3), `docs/paper/emporia.md`.
- **Designed, not built (docs only):** `docs/hermes-notes.md` (one issue draft for the person to post),
  `docs/proposals/agent-bug-reports.md`, `backup-and-room-transfer.md` and `multi-hop.md`; the
  last two are for after the merge.

## The 11-02 audit (P261–P268, on `claude/handoff-4rte3l`) — not tested by hand yet

An audit of the branch (security and design), then fixes, each a commit with tests; details per
item in `docs/security-log.md` (P261–P268). Unit suite 1,927 passed, live suite 10/10.
- **P261** no header trust: identity is only a bearer token; the relay serves no dashboard (`/ui/`
  and `EMPORIA_SERVE_UI` gone). The Hermes tab and the standalone console both sign in with the key.
- **P262** payout wallets signed by their agent; every offer carries the payee's statement, and
  payers (and the signer) pay only a signed address, of the agent pinned under that name.
- **P263/P268** the transparency log fed by SQLite triggers into an outbox, rows followed by key;
  status changes (refunds) logged; salted commitments; no full-table scans; drained in 500-entry
  batches. Table talk, memberships, room invites out of it. Schema 21; the log is rebuilt once.
- **P264** federation: a relay speaks only for itself (origin = the linked URL; `scan_relays`
  holds a relay to the host it was reached at).
- **P265** an operator's approval is bound to a hash of the payment's terms.
- **P266** shorter `DB_LOCK` holds; connections closed; WAL set once.
- **P267** rotation ends the old key's sign-ins; `?token=` redacted from logs; re-pin attempts
  rate-limited; backfilled log entries marked; a test keeps untagged signed payloads distinct.
- **To do by hand:** `docs/manual-testing.md` §1d. After updating, each agent registers its x402
  wallet again (the upgrade drops unsigned ones). `EMPORIA_TRUST_LOCAL_HEADER_AUTH=1` in a profile
  `.env` is ignored now; delete it.
- **Not run here:** the dashboard's `tsc`/`vitest` (npm refuses the git-sourced `srcl` package on
  this machine). Later items from the audit: ROADMAP § From the 11-02 audit.

## Review of P261–P268 (11-02, a short read of the log ingest) — items 1-4 fixed in P269

A spot check of `translog.py` after the audit; not a full review. In priority order. **P269 (11-02)
fixed 1-4** as planned, with two differences: the epoch is a number (1, 2, …) in every signed state
rather than a hash of the first state, and NULL keys (3) are refused by the log's trigger rather than
by a schema migration of 18 tables (`test_every_logged_key_column_is_guarded_on_the_relays_own_schema`).
Schema 22. 5 is in ROADMAP. The plan as written:

1. **A rebuild wipes the log (fix before clients check it, step 2).** `init_schema` drops every
   `translog_*` table when `INGEST` changes (it has, twice), so leaves, signed states and the
   commit secret all go. To a client that kept an old state that is "this relay rewrote its
   log", and private-record salts change, so parties holding old salts can no longer check.
   Plan: give each log an epoch (`log_id` = hash of its first state) in every signed state;
   a rebuild starts a new epoch and the relay signs a hand-over state naming the old epoch and
   its last size/root; keep `commit_secret` across rebuilds; clients treat a new epoch as loud
   but not as a rewrite. A test: rebuild, then the old state is still served and named.
2. **Rows removed while the log was off are never logged as gone.** Turning it on again
   backfills only rows that exist; `translog_seen` still holds the removed ones' digests, so no
   `_gone` entry is made. Plan: on turn-on, also queue every key in `translog_seen` of a
   SOURCES kind (one pass); `_ingest_row` then logs the missing ones as gone. A test: off,
   delete a listing, on, expect `listing_gone`.
3. **A NULL key column silently skips a row.** `_key_sql` joins with `||`, which gives NULL if
   any key column is NULL; `drain` skips a NULL ref. Every key column should be NOT NULL — a
   test that checks each SOURCES key column's schema closes it for good.
4. **`_gone` entries hash a private record's id without salt** (`content_hash(kind, id)`):
   anyone who can guess an id can tell it was removed. Use `commitment_for` for private kinds.
5. **Chain heads count rows each time** (`COUNT(*)` per chain per drain). Fine now (once per
   chain per batch); on a busy room, keep the count in `translog_seen` with the head instead.
6. **Still open from the audit:** see ROADMAP § From the 11-02 audit (steps 2+3 together,
   pinning a seller by the listing's stable id, replacing a recovery commitment, a writer
   thread, splitting large modules, the dashboard's `tsc`/`vitest`).

## Latest stretch (P251–P260, 10-29 to 11-01) — none of it tested by hand yet

- **P251:** fixes from the 10-29 hand test (private topics, room listings, Agora posts).
- **P252:** `emporia update` — pull, then packages, dashboard tab, plugins and relay.
- **P253–P255:** the dashboard checks a message against the key its sender had then, says when
  a key changed and whether a recovery key exists; the relay logs every key change.
- **P256–P259, the transparency log** (schema 18; `src/emporia/translog.py`,
  `routes/translog.py`, `tests/test_translog.py`; plan in
  `docs/proposals/relay-transparency-log.md`, step 1 of 4):
  - one RFC 9162 Merkle log per relay; every table is either logged (public records by id,
    private ones as commitments, chain heads, relay key) or listed in `translog.NOT_LOGGED`
    with the reason — a test enforces this;
  - signed log states every 5 min / 1,000 entries; public read-only `/log/state`,
    `/log/entries`, `/log/proof/inclusion`, `/log/proof/consistency`;
  - optional: `EMPORIA_TRANSPARENCY_LOG=on|off` (default off since P281), chosen in `emporia setup log`,
    `emporia relay host` and the menus. Turned on later, it backfills everything;
  - ~353 bytes and ~41 µs per entry; a proof at 200k entries ~0.5 ms. A VPS is fine.
  - Docs updated: README, ARCHITECTURE, SECURITY, paper §3.7, protocol §4.5/§6/§9, RUNBOOK,
    ROADMAP, skills.
- **P260:** research only — `docs/proposals/robinhood-chain.md` (testnet `eip155:46630`, USDG
  not USDC; x402 blocked on a facilitator and the official token address/EIP-712 details).
- **Proposals for decision:** `docs/proposals/agent-names-and-identity.md` (Emporia as the
  name registry, `name@host`).

### Rough edges — fixed in the 11-02 audit
- The first fold holding `DB_LOCK`: batched (P263, P266). P256–P259 reviewed (P263, P268).

### Questions waiting on the user
- **Names:** adopt `name@host`? Which host (public URL host, or a configured name)? Names per
  Nous account (3?), hold after release (30 days?), the reserved list, a ✓ for Nous-verified?
- **Anchoring the log on a chain (step 4):** which chain (Base Sepolia suggested, or Robinhood
  testnet), a plain transaction with the root in calldata or a small contract, how often
  (hourly?), spend cap, operator pays. Any transaction needs the user's say-so.
- **Robinhood Chain:** add it once the token address and a facilitator are confirmed?
- Note range-based set reconciliation in the federation proposal? It could fit Agora/listing sync
  later; it does not fit logs or chains.

### Next steps, in order
1. The user hand-tests §1c of `docs/manual-testing.md` (includes the log checks); fix failures.
2. Transparency log steps 2 and 3: done (P281; `src/emporia/logcheck.py`, `tests/test_logcheck.py`).
3. Names step 1 (after the decisions above), then log step 4 (chain anchor; needs which chain, who pays).

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

Item 1 is done; item 4's Hermes issues, bug-report design, proposals and status updates are done
(see below). For the cloud agent. Work top-down, one numbered `P…` commit (or a few) per item, tests with each.
Push to `post-hackathon` only when the user says so. Items marked **(machine)** need the user's
Hermes setup; leave those for a person.

### 1. Agent-signed listings — a chain of custody across relays (before the merge) — **done: P240, P241**

Built on `claude/handoff-4rte3l` (not yet on `post-hackathon`). Choices made along the way, for
the user to confirm:
- **The agent is named by `stable_id`, not its handle, inside the signature.** A rename rewrites
  `listings.agent_id` on every row (`_RENAME_COLUMNS`), which would break a signed handle.
- **`/health` gives `relay_url`** (also on a closed relay, for its members): the origin an
  agent signs. The URL an agent reaches a relay at can differ (Docker, a proxy).
- **Bounties and events' listings stay home.** A bounty's state changes after funding, so no
  agent signature can cover it; both are relay-authored and left out of the feed. `scan_relays`
  no longer finds other relays' bounties. ROADMAP § Needs a decision has the option.
- **Pulled rows from before P240 are dropped on upgrade** and come back signed on the next pull.
- **Open games (P241):** the creator signs the terms *after* creating (`POST /sessions/{id}/offer`),
  because the relay fills in defaults and may add a clock. MCP and the SDK offer a public game
  on their own. Only offered games are in the lobby feed.
- Schema 8 (P240), 9 (P241). Three tests in `test_emporia.py` (dashboard not built, store import
  order) fail in a fresh cloud container with or without these changes.

Then, on the user's go-ahead (10-28): **P242** signs Agora posts and comments; **P243** signs a
room's settings, invites and kicks, with a new `check_room` MCP tool (71 tools then). Neither travels
between relays — rooms and Agoras never leave their relay — so these protect against the agent's
own relay. Still unsigned: Agora votes, topics, subscriptions; room joins. Schema 10, 11.

The original plan, kept for reference:

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
   - **Key rotation (P245):** after `emporia agent rotate-key` the agent's current key won't hash to
     its `stable_id`; its signed `key_log` leads from the key that does (`wire.key_verdict`).
   - **Key recovery (P246):** the log may also hold `commit` and `recovery` steps, and after a
     recovery the key that signed an item must be the key `key_at` gives for the time it was
     recorded — read the log through `wire.key_log_windows`, never by walking the steps.
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

`docs/manual-testing.md` §1b (setup and safety, the signer in practice mode), the rest of §2, and
"New since 10-28 (P240–P250)" (signed listings, key rotation and recovery, adverts, two linked
relays, `python scripts/live_relays.py` once on the machine). A forged listing is refused in the
live suite, not by hand.

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

- **Rooms can't be edited or deleted.** **Needs the user's decision** (ROADMAP § Needs a
  decision); propose options, don't build. (The other half of this item, "private + paid is listed
  publicly", was decided 10-28 and built in P249: invite-only rooms are never advertised or listed,
  a paid room only if its creator advertises it; relays pull each other's adverts; the dashboard
  shows host, advertised, and an "On other relays" section.)
- **Hermes issues to draft** (text only; the user posts them): **done**, `docs/hermes-notes.md`: checked against Hermes's code, only
  one is a real issue (a profile's CLI cannot see root-installed plugins).
- **Agents write bug reports; a person files them** (ROADMAP § Before publishing): **design done**,
  `docs/proposals/agent-bug-reports.md`; the smallest first version (a `report_problem` tool and
  `emporia reports list|show`) is not built.
- **Backup and room transfer; multi-hop:** proposals written (`docs/proposals/backup-and-room-transfer.md`,
  `multi-hop.md`), for after the merge. Not built.
- **Name registry and a transparency log (Merkle, optional chain anchoring):** proposals with step plans,
  `docs/proposals/relay-transparency-log.md` and the registry plan in `agent-names-and-identity.md`;
  log first, names on it. Log steps 2-3 built (P281). Log step 1 built (P256-P258: `translog.py`, `/log/*`, every
  record kind, stored subtrees, optional via `emporia setup log`, schema 18); the rest awaits
  the user's decisions.
- **README status and `ROADMAP.md` § Before publishing:** updated for P240-P250; update again as
  items close.

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

Checked against Hermes's code (commit `0c74cc6`); details and the one issue draft in
[hermes-notes.md](hermes-notes.md):
- A plugin's **websockets are refused in any profile but the launch one**: documented in Hermes
  ("does not carry websockets yet"). The tab polls instead (P236). Nothing to file.
- `/api/plugins/*` requests carry **no `?profile=`**: by design; plugins read the
  `data-hermes-profile` tag on their script, as the tab does (P227, P234). Nothing to file.
- `hermes -p NAME plugins enable emporia` **cannot see the tab**, which is installed in the root
  `plugins/` folder, and matches the platform plugin (`platforms/emporia`) instead. The installer
  writes the entry itself (P225). **Worth an issue**; drafted in hermes-notes.md.
- **Agent-to-agent conversations crowd the session list.** Hermes has a per-session `hidden` flag;
  the platform plugin could set it on the sessions it starts. After the merge.

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
