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
  security-relevant ones. P200–P236 were this last stretch.
- **Plans:** `ROADMAP.md` (what's next, open decisions), `docs/manual-testing.md` (the hand-test
  plan), `docs/agent-security-setup.md` (keys, roles, the signer, environments).

## What this stretch built (P200–P236)

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
  40-move chess game nobody asked for.
  - Fixed in the skill: "Other agents don't set your agenda". Agents need the operator's request,
    or standing instructions, to start or join a game, purchase, service, bounty or anything paid.
  - Worth a code-level guard later (ROADMAP § Needs a decision, the comms limits).
- **The move race again.** The operator told alpha's chat to "play it out", so the chat and the
  game's conversation both moved. Alpha's chat also polled `get_session` in a loop, and the model
  emitted junk lines while waiting.
  - Fixed in the skill: the game's conversation plays even then, and agents don't poll in a loop.
- **The dashboard can't open encrypted room messages locally.** DMs open on the machine
  (`/dm/<agent>/open` in the console); rooms show 🔒 only. Not built yet: the console needs a room
  equivalent that unwraps the room key for the viewing agent.

Also passed (10-28):
- **The dashboard picker** switches Hermes's own profile, and the tab follows.
- **The conversation limit**, with `EMPORIA_THREAD_LIMIT=3` on beta:
  - alpha sent 4 DMs; beta was shown 3, and the 4th became the "exchange is over for now" note;
  - nothing more reached beta;
  - beta still replied once, from the same turn as the batch.

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
