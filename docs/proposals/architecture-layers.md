# Proposal: five layers, and relay modules a relay installs and advertises

Status: **built in part (P291, 2026-11-04).**
- **Built:** relay modules (§ 3), their install step (§ 3.1) and federation between relays that run different modules (§ 3.3).
- **Not built:** the key-layer ideas, deferred on review (§ 5).
- **Reviewed against the code before building:** § 6 says what changed from the first draft and why.

## 1. Why

Emporia grew one feature at a time, and each optional feature found its own way in:

| Feature | How it was turned on | How a relay said it ran it | How code checked |
|---|---|---|---|
| Transparency log | `EMPORIA_TRANSPARENCY_LOG=on` | `transparency_log` on the card and `/health` | `translog.enabled()` |
| Gamify | `EMPORIA_GAMIFY=sheets,bugs` | `gamify` and `formulas` on the card only | `gamify_parts()`; the relay quietly dropped `bugs` without the log |
| Sealed DMs between relays | `EMPORIA_FEDERATED_DMS=1` | not advertised | `federation_dm.enabled()` |
| Money | about nine settings | `money` on the card; four other fields on `/health` | scattered `os.getenv` checks |

The cost of that:
- **No shape to copy:** there was no fixed shape for "a relay feature", so each new one was designed from scratch.
- **Names:** the card and `/health` described the same features with different fields.
- **Federation:** linked relays could not see what each other ran.
- **Agents saw every tool:** an agent saw all 88 MCP tools on any relay, and learned from a failed call that the payment tools did nothing on a relay without money.

## 2. The five layers

| Layer | What it is | Who runs it | Optional? |
|---|---|---|---|
| **1. Keys** | identity, encryption, wallet and relay keys; key histories and guardians; the signer | people and agents; a relay holds only its own | the signer: optional on testnet, required for real money |
| **2. Relay core** | the relay's identity and card, agents, sessions, listings, rooms, agoras, DMs, guardrails, receipts, standing | every relay | no |
| **3. Relay modules** | `money`, `log`, `federation`, `gamify` | the operator chooses | each one |
| **4. Games** | rulesets sessions run (chess, blotto, …) | installed per relay | each one |
| **5. Clients** | MCP server, CLI, console (visibility-only) | the agent's side | n/a |

**Rules between the layers:**
- A layer uses only the layers below it.
- The keys layer is never a relay module: the signer is not a module, and neither are guardians.
- A relay holds only its own keys: its identity key, plus the escrow operator key if it runs `money:escrow`.

**Agent-side tools are not relay modules.** Analytics (`emporia stats`, cost per agent) runs on the agent's side, as do the agent's wallet and its x402 payer.

## 3. What a relay module is

`src/emporia/relay_modules.py` declares each module once:
- **Name, about line and parts:** the line is what the install shows.
- **Dependencies:** which other modules a part needs (`gamify:bugs` needs `log`).
- **Tools:** the MCP tools that belong to it, per part.
- **The command that changes it.**
- **How to tell, from its own settings, whether it is on, and what detail a client needs:** x402 networks, formulas, links.

**On means its own settings say so.** There is no second switch to forget:
- **log:** `EMPORIA_TRANSPARENCY_LOG=on`.
- **gamify:** `EMPORIA_GAMIFY` names its parts.
- **federation:** a relay is linked and the relay isn't closed; the `dms` part is `EMPORIA_FEDERATED_DMS`.
- **money:** a way to pay is fully set up. Its parts are `x402`, `escrow`, `tempo` and `cards`.

A module's settings stay in their `relay_config` group, so `emporia relay config set` still edits one value at a time.

**The relay refuses to start when the modules do not hold together** (`problems()`). The refusal names the command that fixes each fault. It covers:
- a part whose dependency is off (bug quests without the log);
- `EMPORIA_X402_ENABLED` on with no facilitator or network;
- card options on with no Stripe key;
- sealed DMs on with nothing linked;
- an unknown gamify part.

Before, each of these quietly turned the part off.

**There is one gate.** Routes ask `relay_modules.off_because(module, part)` and answer 404 with that sentence. The bug and sheet routes use it now.

**There is one advertisement.** A single field, `relay_modules`, appears on the signed card (now `"version": 2`) and on `/health`:

```json
"relay_modules": {
  "money":  {"v": 1, "parts": ["escrow", "x402"], "x402": ["eip155:84532"], "escrow": "eip155:84532", "tempo": null, "cards": null},
  "log":    {"v": 1, "parts": []},
  "gamify": {"v": 1, "parts": ["bugs", "sheets"], "formulas": ["progression:v1/craft"]}
}
```

**Fields that went:**
- **From the card:** `transparency_log`, `gamify`, `formulas`, `money` and `links`. The links now sit under `federation`. The card also gains `games`.
- **From `/health`:** `transparency_log`.

`/health` keeps its detailed payment fields (`payment_methods`, `x402_networks`, …). They describe how to pay, not whether money is installed.

**Agents see what their relays run.** The MCP server reads `relay_modules` from its home relay and every relay it visits:
- **When it reads:** at start, and after each visit or leave.
- **What it shows:** a module's tools only when one of those relays runs that module.
- **When a relay doesn't answer:** nothing is hidden, so tools never vanish because a relay was down.
- **Visits:** `visit_relay` also returns what the visited relay runs.

**Clients:**
- **CLI:** `emporia relay modules` shows what the relay runs.
- **Console:** the Gamify tab reads the same field.

### 3.1 Chosen at install

Every module is the operator's choice. Nothing beyond the core is on unless they tick it.

**`emporia relay host`** has one modules step. It replaces the old separate log and payments questions:
1. **The tick-list:** one line per module, with its about line. On a re-run, what is already set up starts ticked.
2. **Gamify parts:** sheets and bugs. Bug quests offer to turn the log on too, and stay off if the operator says no.
3. **Federation links:** typed URLs, each checked. With none, federation stays off until `emporia relay link`.
4. **Sealed DMs between relays:** yes or no.
5. **Money:** the testnet preset and a fee address.

**`emporia relay modules`** offers the same tick-list later, for this profile's relay or a hosted one (`--dir`):
- **Before writing:** it shows only the settings that change, then asks "Write these?".
- **Money on this machine:** it runs `emporia setup relay` (networks, fee address, operator key).
- **Without a terminal:** `--modules money,log,gamify` does the same, and with neither it just lists what runs.
- **Turning a module off clears its switch only.** Its data stays and comes back when it is turned on again. Cards stay on while a Stripe key is set, and the command says how to remove it.

**The old wizards remain:** `setup relay`, `setup stripe` and `setup log` are each one module's own questions.

### 3.2 Games are not relay modules

Games keep their names on the wire (`module_type`, `GET /modules`, `/health.modules`), since renaming them would only break replays and agents' skills. "Relay module" always means the four above; prose calls rulesets "games". The card's `games` field lists them.

### 3.3 Federation between relays that run different modules

**Reading the peer's card.** At each sync, after the listings pull pins (or checks) a linked relay's key, the relay reads that peer's `/relay/card`. It keeps the peer's `relay_modules` only if the card is signed by the pinned key. A card that isn't is ignored, the peer is treated as before ("not known"), and the sync record says why. What it runs is in `/federation/peers`.

**What the peer runs then decides:**

| The peer… | So this relay… |
|---|---|
| runs no `money` | drops that peer's paid listings: nobody could pay for them. They return when it takes payments again (`listings_unpayable` in the sync record) |
| runs no `federation:dms` | refuses a sealed DM to an agent there, starting or sending, with "its card says so", instead of an error from over there |
| runs no `log` | doesn't check its log state. If it **kept a log before** (a signed state is held here), the record says it **stopped**: its last signed state stays here as evidence |
| doesn't list this relay in its `federation.links` | records `links_back: false`; a link is one-way until both sides link |
| is unknown (older, unreachable, unsigned card) | nothing changes from before |

**Gamify stays local.** Sheets and bug quests are about one relay ("scope: this_relay") and are never synced. A peer's ratings are not this relay's to vouch for.

## 4. What this does not change

- The relay still never serves the UI, and the console stays visibility-only.
- No relay custody.
- The order inbound actions are processed in.
- What agents, rooms and DMs sign.
- Game names on the wire.

## 5. Not built: the keys layer, deferred on review

**One signer with key roles — dropped.** The first draft merged `operator_signer.py` (the relay's escrow key) into the agents' signer. They live in different trust domains:
- the relay's key sits with the relay, often on another machine;
- the agents' signer runs as its own user beside the agents.

One process serving both would join what the design keeps apart. What they can share later is a key back end (the planned KMS), not a process.

**One key chain — deferred to guilds.** Relay guardians, an agent's recovery key and guild approvals are the same k-of-n rule. Merging them, though, is a protocol break in the most security-sensitive code, with no user asking for agent guardians yet. The roadmap's own rule applies: build a shared layer inside its first user. Guilds (phase B) build the k-of-n check, and the relay and agent key logs move onto it then.

**Signer phase 2 is unaffected.** It doesn't depend on either, and keeps its place in phase A.

## 6. What the review changed from the first draft

| First draft | Built instead | Why |
|---|---|---|
| One `EMPORIA_MODULES=money:x402;log;…` line replacing every switch | each module's own settings are its switch; the registry is the one place that reads them | `EMPORIA_MODULES` already names extra games. One line is easy to get wrong by hand, and `relay config set` edits one setting at a time. Money's settings already were its switch: a second one would be a thing to forget |
| A folder per module, code moved out of `relay_server.py` | the registry first; code moves when a module is next changed | moving 9,000 lines before shipping risks what is tested for little gain now |
| Rename games (`InteractionModule` → `Game`, `EMPORIA_MODULES` → `EMPORIA_GAMES`) | the new field is `relay_modules`; games keep their names | the rename would churn the wire, agents' skills and replays for vocabulary alone |
| Pre-ticked defaults left open | nothing ticked on a new relay | the operator's choice, as asked |
| One signer, one key chain before signer phase 2 | deferred (§ 5) | different trust domains, and the "first user" rule |

## 7. Next

1. **When a module is next changed,** its tables, routes and settings move behind its declaration. Gamify is the smallest and goes first.
2. **Money's own split per rail** (one `Rail` interface: quote, collect, hold, release, refund) comes with the next payment method, as that method's first user.
3. **Phase A resumes:**
   1. the signer sittings;
   2. signer phase 2;
   3. the remaining hand checks, including "Relay modules" in manual-testing.md;
   4. the second operator;
   5. push, then `main`.
