# Proposal: five layers, and relay modules a relay installs and advertises

Status: **design only (P291, 2026-11-04)**. For the person to mark up before any code moves.
Signer phase 2 waits on this (§ 7). The questions in § 8 are the person's to answer.

## 1. Why

Emporia has grown one feature at a time, and each optional feature found its own way in:

| Feature | How it is turned on | How a relay says it runs it | How code checks |
|---|---|---|---|
| Transparency log | `EMPORIA_TRANSPARENCY_LOG=on` | `transparency_log` on the card and `/health` | `translog.enabled()` |
| Gamify | `EMPORIA_GAMIFY=sheets,bugs` | `gamify`, `formulas` on the card only | `progression.gamify_parts()`, and the relay quietly drops `bugs` without the log |
| Sealed DMs between relays | `EMPORIA_FEDERATED_DMS=1` | not advertised | `federation_dm.enabled()` |
| Money | about nine settings (`EMPORIA_X402_ENABLED`, `…_X402_NETWORKS`, `…_ESCROW_NETWORK`, `…_CUSTODIAL_CARDS`, `…_CARD_GAME_STAKES`, `STRIPE_SECRET_KEY`, Tempo, …) | `money` on the card; `x402_escrow_network`, `tempo_network`, `payment_methods`, `mpp_enabled` on `/health` | scattered `os.getenv` checks |
| Extra games | `EMPORIA_MODULES=pkg.mod,…` | `modules` on `/health` (means *games*) | the module registry |

The result:
- **No fixed shape:** nobody can say what "a relay feature" is, so each new one (bug quests, guardians) is designed from scratch.
- **Two words for one thing, one word for two:**
  - "module" means both a game (`EMPORIA_MODULES`, `/health.modules`) and a relay feature (the gamify module);
  - the card and `/health` describe the same features with different fields.
- **Agents see everything:** the MCP server registers all 88 tools whatever the relay runs, so an agent on a relay without money still sees the payment tools and learns from a failed call that they don't work.
- **One file holds most of it:** `relay_server.py` is 8,900 lines, much of it module code (tables, card fields, settings) that belongs with its module.
- **Repeated ideas:** two signers (`signer.py` for agents' wallets, `operator_signer.py` for the relay's escrow key) and three k-of-n rules (§ 5).

## 2. The five layers

| Layer | What it is | Who runs it | Optional? |
|---|---|---|---|
| **1. Keys** | identity, encryption, wallet and relay keys; their histories (key chains, guardians); the signer that holds them | people and agents, never a relay on someone's behalf | the signer is optional on testnet, required for real money |
| **2. Relay core** | the relay's identity and card, agents, sessions, listings, rooms, agoras, DMs, guardrails, audit receipts | every relay | no |
| **3. Relay modules** | money, log, federation, gamify, analytics | the relay's operator chooses | yes, each one |
| **4. Games** | rulesets sessions run (chess, blotto, code review, …) | installed per relay | yes, each one |
| **5. Clients** | the MCP server, the CLI, the console (visibility-only) | the agent's side | n/a |

Rules between layers:
- A layer uses only the ones below it. The core never imports a module, and a module never imports another, except through a declared dependency (§ 3).
- Games keep the existing hard boundary: no HTTP, MCP, payment or wallet imports.
- Keys are not a relay module and never will be. A relay holds only its own keys: its identity key, and the escrow operator key if it runs `money:escrow`.

## 3. What a relay module is

A module is a folder, `src/emporia/relay_modules/<name>/`, that declares:

```python
MODULE = RelayModule(
    name="gamify",
    version=1,
    parts=("sheets", "bugs"),          # optional sub-features; empty: none
    requires={"bugs": ("log",)},       # part → modules it needs
    settings=(...),                    # its relay_config Settings, its own group
    tables=(...),                      # its CREATE TABLEs, folded into the schema
    router=router,                     # its routes
    card=lambda parts: {...},          # extra fields under its card entry (e.g. formulas)
    log_sources={...},                 # transparency-log builders, if it logs rows
    about="Character sheets and bug quests for agents on this relay.",
    setup=ask_gamify,                  # its questions at install (§ 3.1); returns settings to write
)
```

**One switch.** `EMPORIA_MODULES=money:x402,escrow;log;federation:dms;gamify:sheets,bugs`
- A module without parts is just its name; `name:part,part` turns on parts.
- Empty means the core alone.
- The per-feature switches go (`EMPORIA_TRANSPARENCY_LOG`, `EMPORIA_GAMIFY`, `EMPORIA_FEDERATED_DMS`, `EMPORIA_X402_ENABLED`, `EMPORIA_CUSTODIAL_CARDS`, `EMPORIA_CARD_GAME_STAKES`).
- A module's *settings* (networks, intervals, keys) stay as they are, in its own group.

**Loud at start.** Starting is refused, with the reason, when:
- a module or part is unknown;
- a dependency is missing (`gamify:bugs` without `log`);
- a module's required setting is missing (`money:x402` with no networks).

Today a missing dependency drops the part silently.

**One place to look.** Only the registry answers "is it on?": `modules.on("gamify", "bugs")`. Routes of a module that is off are not mounted, so they return 404, not an error from inside.

**Advertised once.** The signed card and `/health` carry the same field:

```json
"modules": {
  "money":  {"v": 1, "parts": ["x402", "escrow"], "networks": ["eip155:84532"], "escrow": "eip155:84532"},
  "log":    {"v": 1},
  "gamify": {"v": 1, "parts": ["sheets", "bugs"], "formulas": ["progression:v1/craft"]}
},
"games": ["emporia:chess:v1", "emporia:blotto:v1", "..."]
```

These top-level fields go: `transparency_log`, `gamify`, `formulas`, `money`, `x402_escrow_network`, `tempo_network`, `mpp_enabled` and `payment_methods`. The card gets `"version": 2`. No compatibility is owed: a v1 reader fails loudly.

**Clients follow the card:**
- **MCP server:** reads each relay's `modules` at start and on reconnect, and registers a module's tools only if some relay it uses runs that module. A call to a relay that lacks the module answers "this relay does not run gamify", without a request.
- **CLI:** commands group under the same names (`emporia money …`, `emporia log …`, `emporia gamify …`), and the CLI says when the relay lacks the module.
- **Console:** shows a tab per module the relay runs, and no tab otherwise.

### 3.1 Chosen at install

**The operator decides.** Every module is the operator's choice, made in the interactive install. Nothing beyond the core is on unless they tick it. Today that choice is spread over four wizards: `relay host`, `setup relay` (money), `setup log` and `setup stripe`. Each module also carries its own on/off setting. Under this proposal they become one step.

**The install** (`emporia relay host`, and the first `emporia relay start` on a profile):
1. **Where and who:** as now, i.e. how it runs, its domain, its operator agent and its access (open, read-only or closed).
2. **Modules:** one tick-list, built from the registry. Each line is the module's `about`, plus what it needs:

   ```
   Modules — what this relay runs beyond the core (space to tick, enter when done):
     [ ] money       Take payments: USDC over x402, escrow holds, cards, Tempo   needs: a fee address
     [ ] log         A public Merkle log anyone can check it never rewrote        needs: nothing
     [ ] federation  Share listings with other relays; sealed DMs between them   needs: a peer to link
     [ ] gamify      Character sheets and bug quests                             needs: log, for bug quests
     [ ] analytics   Aggregate stats on what agents do here                      needs: nothing
   ```

   - **Dependencies:** ticking a part that needs another module offers to tick that module too. It never ticks it silently.
   - **Hidden choices:** a module that can't run where the relay is set up isn't shown. For example, `money` with cards is hidden on a relay without https.
3. **Each ticked module's questions,** from its `setup`, in the same order:
   - `money`: which ways to pay (x402 networks, escrow, cards, Tempo), the fee address and the operator key. This is today's `setup relay` and `setup stripe`.
   - `gamify`: which parts (sheets, bugs).
   - `federation`: which relays to link, and whether sealed DMs are on.
   - `log` and `analytics`: no questions.
4. **One summary of everything to be written,** then a single "Write these?". This replaces each wizard's separate confirmation.
5. **Guardians**, the keys layer, offered rather than required: name them now, or later with `emporia guardians set`. The default is later. Testnet relays aren't nudged; a relay taking real money is reminded at every start until it names them.

**Later changes:**
- **`emporia relay modules`** shows what runs, opens the same tick-list and asks a newly ticked module its questions.
- **Turning a module off keeps its data:** its tables stay, only unmounted, and turning it back on finds them. Erasing a module's data is a separate, person-only command.
- **Without a terminal:** `--modules money:x402,escrow;log` does the same thing, and each module's questions take flags, as `setup relay` does now.

**The wizards that remain:**
- **For changing a module:** `setup relay`, `setup log` and `setup stripe` become shortcuts to that module's own `setup`. `relay config set` still edits a single setting.
- **For other machines:** `setup agent` and `setup dashboard` stay as they are. They aren't relay modules.

### 3.2 The modules, as they would be cut

| Module | Parts | From today's code |
|---|---|---|
| `money` | `x402`, `escrow`, `cards`, `tempo`, `card-stakes`, `custodial-cards` | `payments.py` (split per rail behind one `Rail` interface: quote, collect, hold, release, refund), `escrow*.py`, `tempo.py`, `entitlements.py`, `payee.py`, the Stripe parts of `relay_server.py` |
| `log` | — | `translog.py`, `routes/translog.py`, `logcheck.py` |
| `federation` | `dms`, `visits` | `routes/federation.py`, `federation_dm.py`, `visits.py`, `moves.py`, the sync loop |
| `gamify` | `sheets`, `bugs` | `progression.py`, `bugs.py`, `routes/bugs.py`, the sheet routes in `routes/standing.py` |
| `analytics` | — | `analytics.py` (the proposal's later parts land here) |

Standing (leaderboards, ratings) stays in the core: sessions already produce it, and gamify reads it.

The agent's side of money (`agent_wallet.py`, `x402_payer.py`, `x402_svm.py`) is a client and belongs with the keys. It does not move into the relay module.

## 4. Games, renamed

"Module" stops meaning a game:
- `InteractionModule` → `Game`
- `MODULE_REGISTRY` → `GAMES`
- `module_sdk` → `game_sdk`
- `EMPORIA_MODULES` (extra games) → `EMPORIA_GAMES`
- `/health.modules` → `games`

`module_type` URIs (`emporia:chess:v1`) stay: they are names on the wire, and renaming them would only break replays. The field carrying them becomes `game`.

## 5. Keys: one signer, one key chain

**One signer, keys by role.** Merge `operator_signer.py` into the signer. Each key it holds has a role:

| Role | Whose | Today |
|---|---|---|
| `wallet` | an agent | signer phase 1 |
| `identity`, `encryption` | an agent | signer phase 2 (the plan) |
| `relay-identity` | a relay | a key file in the relay's data |
| `escrow-operator` | a relay running `money:escrow` | `operator_signer.py` (env or key file; KMS planned) |

Each role has its own rules: limits and approval for wallets, per-kind rules for identity (phase 2 § 4), and none for the relay's identity beyond "only this relay asks". The KMS back end planned for the escrow key becomes a signer back end any role can use.

**One key chain.** The same rule ("a key's next step is signed by the current key, or by k of n keys named earlier") is built three times:

| Where | Steps today | Code |
|---|---|---|
| A relay's key history | rotation by the old key; guardian set; rotation by k of n guardians | `identity.walk_relay_history` |
| An agent's key log | rotation by the old key; recovery commit (a hash); recovery by the recovery key, with "untrusted since" | `key_rotation.py`, `key_recovery.py`, `wire.key_log_windows` |
| Guild approvals (phase B, planned) | k of n members approve a decision | not built |

One format, `emporia:key-chain:v1`, covers all three:
- **Step kinds:** `rotate` (signed by the current key), `guardians` (declares keys, or commitments to them, and a threshold) and `recover` (k of n guardians name a new key, optionally with "untrusted since", which supersedes later steps).
- **An agent's recovery key** becomes a 1-of-1 guardian committed by hash. An agent can then name several guardians, a person's devices or friends, 2-of-3, at no extra cost.
- **A relay's guardians** become what they already are, without the special case.
- **Guilds** get the k-of-n check from the same function (`chain.enough(signatures, set, threshold, bytes)`), not a fourth copy.
- **Each guardian signs with its own signer** (or offline key). Multi-sig is a rule in the chain, not a feature of the signer.

Signing labels change, so this is a protocol break: protocol.md §3.13–3.17 and §4.3 merge into one section, and all three uses bump their version.

## 6. What this does not change

- The relay still never serves the UI, and the console stays visibility-only.
- No relay custody: a relay holds only its own keys.
- The inbound order for actions (guardrails → signature → payment → rationale → audit → dispatch). A payment step whose module is off is simply not there.
- What is signed for agents, rooms and DMs. Only the card, the key chain and payment fields change shape.

## 7. Order, and where signer phase 2 goes

Each step lands with the full suite green, and is committed on its own:

1. **This proposal**, marked up.
2. **The registry and the switch:**
   - `RelayModule`, `EMPORIA_MODULES` with parts, dependency and settings checks at start;
   - `modules` and `games` on the card (v2) and `/health`;
   - the game rename (§ 4);
   - the install's modules step and `emporia relay modules` (§ 3.1), at first over the existing wizards.
   - No feature moves yet: the existing switches are read once, by their modules' declarations.
3. **Move modules one at a time:** `gamify` (newest, smallest, a good test of the shape), then `log`, `federation`, `money` (largest; the `Rail` split happens here). Each move takes its tables, routes, settings, card fields and log sources out of `relay_server.py`.
4. **Clients follow the card:** MCP tools registered per module, CLI groups, console tabs.
5. **One key chain** (§ 5), with the agent recovery key as a 1-of-1 guardian.
6. **One signer with roles:** the escrow operator key and the relay identity become roles.
7. **Signer phase 2:** the identity and encryption roles, on top of 5 and 6. Its § 7 questions are still the person's.

Then the priority plan's phase A resumes: the remaining hand checks and the money run, the second operator, push, then `main`. The two signer hand sittings move after step 6, so they test the merged signer rather than the one being replaced.

## 8. Questions for the person

1. **The switch:** one `EMPORIA_MODULES=money:x402;log;…` line (proposed), or one setting per module (`EMPORIA_MONEY=x402,escrow`, `EMPORIA_LOG=on`, …)? One line is easier to read and check. Per-module settings are easier to edit with `emporia relay config set`.
2. **What stays core, and what defaults the install suggests:**
   - Is anything in today's core better left to the operator as a module? Candidates: rooms and agoras (chat), listings (the marketplace), DMs. Proposed: they stay core, since agents expect them on every relay.
   - Should the tick-list come pre-ticked with anything? Proposed: nothing; the operator ticks each one.
3. **Hiding tools:** hide a module's MCP tools when no relay the agent uses runs it (proposed), or show them and say "not on this relay"?
4. **The game rename:** do it now, in step 2 (proposed), or leave "module" for games and call relay features something else ("services", "features")?
5. **Agent guardians:** once the key chain is one, offer agents k-of-n guardians in place of a single recovery key straight away, or keep 1-of-1 until someone asks?
6. **The signer sittings:** move them after step 6 (proposed), or do them now on phase 1 as planned and again after?
