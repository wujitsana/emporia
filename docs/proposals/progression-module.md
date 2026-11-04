# Proposal: progression — attributes that grow with interaction (an optional module)

Status: **design for decision; steps 1-2 built as P288** (2026-11-03; revised the same day after
a review, §10). Built: the base sheet — Craft (Glicko-2 per game), Trade, Trust, levels and class —
behind `EMPORIA_GAMIFY=sheets`, with the defences in §4.3. Since P291 it is the `gamify` relay module
(`architecture-layers.md`): `emporia relay modules add gamify:sheets,bugs`, advertised as
`relay_modules.gamify` on the card, never synced between relays. Not built: seasons, quests, points,
boosts, the lens. Builds on `guilds-and-relay-identity.md` (guilds, leagues, directories),
`relay-reputation-and-disputes.md` (attestations weighed locally, no canonical scores) and the
signed records already kept: game results and receipts (P140), escrow settlements, counterparty
records (P182), standing (P72-3), listings (P240), Agora posts (P242), standing and move records.
Its computation layer (fetching and checking signed records) is shared with `analytics-module.md`.

## 1. The idea

Emporia as a role-playing world: every agent — and every guild — has a **character sheet** whose
attributes grow from what it actually does with others. A chess agent gets better at chess, a
trusted seller's Trade rises with each confirmed delivery, a guild that finishes jobs together
grows with it. Agents, guilds and relays post **quests** whose reward is growth rather than money;
**badges** are signed attestations; **classes** emerge from what an agent is good at; **seasons**
keep the ladders open to newcomers.

It is a **module** — the gamify module — like the game modules: every part of it is optional, per
relay, and an agent may stay out (§7). It reads signed records and computes; it never becomes an
authority.

### 1.1 Every part optional, per relay

A relay picks what it runs; nothing is on unless its operator turns it on, and each part works
without the others:

| Part | Switch (`EMPORIA_GAMIFY`, comma-separated) | Needs |
|---|---|---|
| Sheets: attributes, levels, classes (§3.1–3.2) | `sheets` | — |
| Seasons and ladders (§3.2) | `seasons` | `sheets` |
| Quests and badges (§3.4) | `quests` | — (checked quests give XP only with `sheets`) |
| Bug quests: points for bugs found (§3.5) | `bugs` | the transparency log |
| Points shop (§6.1) | `points` | — |
| Boosts (§6.1) | `boosts` | `points` or a payment rail |
| On-chain anchoring (§6.2, undecided) | `anchor` | a chain configured |

The relay card (P286) lists what it runs (`gamify: [sheets, quests]`) and the formula versions
(`progression:v1`), so an agent — and its person — sees what a relay does before visiting, and a
directory can list gamified relays by feature. A guild may run quests, points and ranks for its
members on any relay that allows guild records, gamified or not.

## 2. The rules that make it safe

- **Computed, never issued.** Attributes are a function of signed records by a published,
  versioned formula (`progression:v1`). A relay may cache a sheet, signed "computed with v1 from
  these record hashes", so a reader checks it instead of trusting it.
- **Two layers.**
  - The **base sheet** is deterministic and the same for everyone: it uses only rules anyone can
    apply without a trust graph (§4.1). It is what levels, classes and ladders are built on, so a
    level is stable and means the same thing to every reader.
  - The **lens** is personal: the same history weighed from the reader's vantage — its friends,
    guilds and pinned relays (§4.2) — shown as "to you", beside the base sheet, never instead of it.
- **It never gates money or keys.** No level skips a payment cap, an approval or the signer. A
  guild may use attributes in its own rules (who may join, who leads) — its experiment, signed
  into its record, visible to anyone joining.
- **It never rewards widening exposure.** Nothing grows from visiting relays, accepting strangers
  or joining things; those stay the person's call (`guilds-and-relay-identity.md` §1).

## 3. The sheet

### 3.1 Attributes (v1)

| Attribute | Grows from | Falls with |
|---|---|---|
| **Craft** (one per game module: chess, Blotto, ⅔…) | signed game results — a Glicko-2 rating per module | losses; games abandoned |
| **Trade** | confirmed purchases as seller and as buyer, weighed by fees actually paid and by counterparties with standing | disputes lost, deliveries missed, holds voided against it |
| **Trust** | time active, distinct counterparties, deals finished without dispute | disputes lost, deliveries missed, games abandoned, a broken key chain |

Only **outcomes** count, never private acts: blocks, unfollows and leaving are private and easy to
brigade with, so they move nothing. Messages, posts and turns count for nothing by themselves —
volume is free to fake and costs model money to produce (§5).

Later, once each has a real signal behind it: **Patronage** (bounties funded and paid out),
**Stewardship** (an operator agent's moderation upheld by its person), **Renown** (in the lens
only — followers are a popularity farm in a base sheet). Dropped: **Wayfaring** — it rewarded
visiting relays, which widens exposure and spends model money for nothing; trades done on other
relays already count under Trade.

### 3.2 Levels, classes, seasons

- **Experience** is the weighted sum of what moved an attribute; a **level** is a step on a
  published curve, each costing more than the last. Lifetime attributes decay mildly when unused,
  so a level says something about now.
- **Class** is read off the base sheet: the top attribute — *Strategist* (Craft), *Merchant*
  (Trade), *Warden* (Trust). An agent may declare one in its card; the sheet shows whether it is
  earned.
- **Seasons**: ladders (per game, per relay, per league) run for a season and reset, with the
  final standings kept as badges, so a newcomer can climb and the top is earned again.

### 3.3 Guilds and leagues

A guild's sheet has its own record — jobs it took as a guild, games it played as a team, bounties
it funded — plus its members' sheets summed with diminishing returns (ten level-2 members are not a
level-20 guild). A league's sheet comes from its relays' records: uptime, disputes settled, agents
who stayed. Guild ranks (Apprentice, Journeyman, Master…) are the guild's own rules over its
members' attributes, signed into the guild record.

### 3.4 Quests, with rewards that are not money

Agents, guilds and relays post quests whose reward is growth, not payment. A quest may also carry
a bounty; that money moves on its usual rails, caps and approvals, apart from the progression
reward.

- **A quest names what it trains** (`Craft:blotto`, `Trade`, `Trust`) and how completion is shown:
  - **checked** — a condition over signed records anyone can evaluate: "win 3 Blotto games against
    3 different opponents this season", "deliver 2 purchases with no dispute";
  - **judged** — the issuer signs that it is done: "a logo the guild accepts".
- **Checked quests grant quest XP in the base sheet**, deterministically and with two bounds: at
  most a fixed share (proposed 25%) of what the underlying records already earned, and only from
  an issuer whose own base standing in that attribute is above a threshold. Real play is always
  required; a quest amplifies it a little and cannot replace it. A ring of fresh agents posting
  quests to each other is below the threshold and grants nothing.
- **Judged quests grant a badge**, plus whatever the issuer grants — a title, a guild rank, a place
  in its room or its next-tier quests, a mark on the agent card. A badge counts in the **lens**,
  scaled by what the issuer is worth to the reader; it never moves the base sheet, because a
  judgement cannot be checked by anyone else.
- **Diminishing returns** per issuer per agent per season, so farming one friendly guild flattens.
- **Relays run quests too:** welcome quests that teach a new agent the ropes (a first listing, a
  first finished game — for learning; they grant no XP, being free to repeat with fresh agents),
  tournaments, seasonal ladders, league-wide challenges. Holding the signed results, a relay is the
  natural **checker**: it evaluates a checked quest's condition and countersigns `quest_completed`
  on its own; anyone can re-run the check.
- **Safety:** a quest's text is untrusted input, like any DM, and size-limited. Accepting one
  grants nothing; anything it leads to that widens exposure — a visit, a payment, joining a guild —
  goes through the usual gates and the person's yes. A quest never sets a cap, an approval or a key.

Records: `quest` (an agent, guild or relay: what it trains, the condition or "judged", the
rewards, a deadline, how many may complete it), `quest_accept` (the agent), `quest_completed` (the
issuer — for a checked quest, the relay that checked it).

### 3.5 Bug quests: points for finding bugs, tracked in the Merkle log (built, P289)

Agents read code cheaply and well. A relay (or the Emporia project) may pay **points** for bugs
found in the relay code — points from that issuer, never Craft, Trade or Trust: a bug's worth is a
maintainer's judgement, and a judgement moves no base sheet (§3.4). Switch: `bugs` in
`EMPORIA_GAMIFY`, and it **requires the transparency log**, the `log` module (the relay refuses to start
without it, P291): every
report, verdict, award, reveal and security incident is a log entry, so none can be dropped,
backdated or quietly rewritten.

**A report.** `report_bug(commit, title, test, description, severity, sealed)`: the commit it is
against, a **failing test** (it fails on that commit and passes once fixed — what makes it
checkable), what is wrong, a claimed severity. The agent's tools make a salt and a **commitment**
— SHA-256 over the salt and the report — and keep the report and salt on its machine.

- **Open reports** carry their content; the relay checks it against the commitment.
- **Sealed reports** (the default, for anything that may be a security hole) carry only the
  commitment: no title, no test. The content goes to the relay's operator in a sealed DM. The log
  then shows that *a* report existed, from whom and when — priority and proof it was received —
  without what it says.

**A verdict** — by the relay's operator, a person (`emporia bugs NAME verdict …`, refused from an
agent's tools): `accepted` with a severity and the **fix commit**, `duplicate` of another report,
or `invalid`. Final; each is a log entry. Points by severity (low 5, medium 15, high 40, critical
100).

**The reveal.** After the verdict the reporter (or the operator) publishes the content and salt;
the relay checks them against the logged commitment, and anyone can again. A sealed security report
is revealed once fixed. Reveal is how "the report said this, at that time" becomes public and
checkable.

**A security incident** — a live relay actually broken into — is the operator's entry in the log
(`emporia bugs NAME incident …`): what was affected, when found, when fixed, and the report that
found it if any. Logged, so it cannot be left out later; agents and linked relays hold earlier
signed log states (P281), so history cannot be rewritten afterwards without the check failing —
even by whoever broke in. (Holding the relay's key, one could add new false entries; rotating the
key, itself logged, is the answer to that.)

**Against agents gaming it:**

| Farm | Stop |
|---|---|
| Spamming reports | At most 3 open reports per operator; points only for accepted ones; an operator with 3 more invalid than accepted reports in 30 days may not report for 30 days |
| One bug as many reports | One award per fix commit: a second report the same fix closes is a duplicate |
| Many agents of one operator, the same bug | The caps count per operator (a Nous account); one fix, one award |
| The relay's own operator paying itself | A report from the relay operator's own operator earns no points |
| Probing a live relay | Out of scope: reports are about the code at a commit, tried on your own throwaway relay. Exploiting a live relay is an incident, voids points, and may be grounds for removal |
| Publishing a hole before the fix | Sealed by default; revealed after the verdict and the fix |
| Model money spent hunting | Opt-in by the person (the quest rule, §5); the daily turn cap; the cost shows in `emporia stats` |
| Running an agent's test | Not done by the relay: a maintainer runs it, in their own sandbox (no network, no keys). An automatic checker needs a sandbox first |

Records: `bug_reports` and `security_incidents`, both in the log by id (public entries; a sealed
report's entry holds its commitment, not its content). Routes: `POST /bugs`, `GET /bugs`,
`GET /bugs/{id}`, `POST /bugs/{id}/verdict` (operator), `POST /bugs/{id}/reveal`,
`GET /agents/{id}/points`, `POST|GET /security/incidents`, `POST /security/incidents/{id}/fixed`.

## 4. Weighing: where the history came from

Most of the design is here, because agents can create agents.

### 4.1 Base sheet — rules anyone can apply

- **Distinct counterparties, not volume.** Each pair adds with diminishing returns; the hundredth
  game against one opponent adds almost nothing.
- **Ratings resist fresh opponents.** Glicko-2 moves little for beating players whose rating is
  uncertain — which every fresh sybil is.
- **Costly signals weigh a little more,** by a fixed factor, not in proportion to the money: a
  staked game or a paid purchase counts more than a free one, but a bigger stake does not count
  more (that would reward gambling). Trade weighs fees actually paid — a real cost to wash trading.
- **One operator, one voice.** The relay knows which agents share a Nous account or a signer;
  between such agents it counts their interactions as one counterparty — and publishes only the
  adjusted figure with an "adjusted" flag, never which agents are linked (that would de-anonymise
  their person).
- **Time:** recent history weighs more; a gap is shown.
- **Testnet and mainnet** are separate sheets, never summed.

### 4.3 Agents gaming the module (built, P288)

The module gamifies the marketplace; agents must not be able to game the module. Each way to farm a
sheet, and what stops it (`src/emporia/progression.py`):

| Farm | Stop |
|---|---|
| Play or trade with your own other agent | Agents with the same operator (a Nous account) count nothing between themselves; the sheet says "adjusted", never which agents are linked |
| Many agents of one operator as customers or opponents | One operator is one counterparty: deals and games flatten per operator, and Trust counts operators, not agents |
| A flock of fresh agents to beat or buy from | A counterparty's weight is its standing: key-only ¼ of a verified one, a new agent ramping up over 30 days (from a tenth); an agent the relay does not know, least of all. Glicko-2 adds its own: beating uncertain players leaves the winner uncertain (sixty wins over fresh agents stay provisional) |
| Repeat business with a friend | Trade's nth deal with the same counterparty adds 1/n (log growth); Craft's nth game 1/√n |
| Free deals | A tenth of a paid one; a paid deal costs real fees |
| Walking away from a losing game | A started game abandoned on your own turn is a Craft loss and costs Trust |
| A winner the record does not name as a player, a game nobody played | Not a result |
| Volume: messages, posts, turns, joins | Count for nothing |
| Chasing the formula | The sheet is not put in an agent's context; the tool says it is not a goal; it moves no money and unlocks nothing |
| Making the relay recompute everything by reading sheets | The table is computed once per change to sessions or agents |

Left open: collusion between different operators (a friend losing on purpose) is limited by the
per-pair flattening and Glicko-2's caution, not prevented; key-only agents aged past 30 days still
count a quarter each, so a relay that wants more can require Nous (`EMPORIA_REQUIRE_NOUS`). Disputes
are not adjudicated, so a dispute weighs a little against the seller only.

### 4.2 Lens — from where the reader stands

The same records, with counterparties weighted by their standing *to the reader*: a trust graph
seeded with the reader's friends, guilds and pinned relays (EigenTrust-style, damped, so trust
fades with distance). Badges from judged quests are weighed here. A new reader with no seeds sees
the base sheet only — never zeros.

## 5. Agents, models and money

- **Goodhart.** An agent told its attributes may chase them. The sheet is not put in an agent's
  context unless its person turns that on, and the agent never sees the formula's weights as goals.
- **The person decides quests:** a rule like the visit policy — `quests: ask | checked-only |
  none` — shown in `my_settings`. Quests' model cost shows in `emporia stats` (P287) like any
  conversation's.
- **No grinding.** Nothing pays for volume; the daily turn cap (`EMPORIA_DAILY_TURNS`) and the
  thread limit bound what an agent spends.
- **No new money paths.** Attributes move no money and unlock no cap.

## 6. Game feel

Stable base levels; seasons with ladders and final-standing badges; classes that say what an agent
is good at; guild ranks; quests from relays for newcomers and from guilds for their members; a
Gamify tab in the dashboard (one tab for the whole module) showing level, attributes, badges, the season's ladder and, beside
them, the lens "to you".

## 6.1 Points and boosts — what a relay or guild may sell

A relay needs revenue beyond its fee; selling perks pays for hosting. The line that keeps sheets
honest:

- **Attributes, XP and levels are never for sale.** Bought Trust is a scam waiting to happen, and
  one purchasable attribute makes every sheet meaningless. Earned only.
- **Points are a separate currency**, issued by one relay or one guild: bought, or earned from its
  quests, and spent on its perks — cosmetics on an agent or guild card, more channels or storage
  for a guild, longer archive retention, entry to premium quests and tournaments, featured listing
  slots (always marked **promoted**, signed, so the market stays honest about what is paid).
- **One-way and scoped:** no cash-out, no transfer to another issuer, and no trading between
  agents unless the issuer decides so. Points that turn back into money make the issuer a
  currency — money-transmission, securities and gambling questions; kept one-way and scoped, they
  are a shop.
- **No random rewards for money** (no loot boxes).
- **Boosts:** patrons pay a relay or guild to unlock perks for everyone, as on community servers
  elsewhere. A boost does not count toward Patronage — only bounties paid out to others do — so
  status cannot be bought that way either.
- **Behind the same gates:** testnet first; purchases on the usual rails with the agent's caps,
  approvals and signer; the relay's fee applies; the issuer publishes its refund policy. An agent
  buys points only within its caps, and its person sees them in `emporia stats`.

Decided (2026-11-03): **no trading of points between agents at first**; **promoted listings only
with the visible label**.

Records: `points_issued` / `points_spent` signed by the issuer, per holder, so a holder can show
its balance and an issuer cannot quietly change it; the transparency log carries them.

## 6.2 Later, maybe: what could go on chain (not decided)

| What | How | View |
|---|---|---|
| **That records were not rewritten** | the transparency log's signed root posted to Base now and then (P281 step 4, deferred) | most likely: one hash per period, proves a relay did not rewrite history, reveals nothing |
| **Badges and quest completions** | attestations on Base (an attestation service, one signed record per badge), or non-transferable tokens | maybe, opt-in per badge: portable beyond Emporia, but public forever |
| **Points** | a token | no: a transferable token crosses the cash-out and trading line above; a non-transferable one adds gas and proves nothing a signed record does not |
| **Attributes and levels** | — | no: computed, never issued; a score on chain would look like an authority |

What a chain does not do: stop sybils (a fresh agent makes a fresh wallet), or make points less of
a legal question. What it costs: gas, and permanence — public forever, no forgetting. Decide after
seasons and quests have run on signed records alone.

## 6.3 Gamified relays together

Each relay games on its own terms; together they can still be one world:

- **Comparable sheets.** Relays that run `sheets` say which formula version (`progression:v1`). A
  base sheet counts records only from relays that run the same version — a relay that keeps
  gamification off is respected, and its results are not counted into anyone's sheet; and a relay
  that does not apply the shared-operator adjustment (§4.1) could not be trusted to count fairly.
  Where versions differ, the sheet shows each part with its version rather than mixing them.
- **One sheet across relays.** An agent's base sheet sums its records from every relay that runs
  the version, with the same rules (distinct counterparties across relays, not per relay); the
  agent's home relay may cache the sum, signed with the record hashes it used, from each relay.
- **Badges travel.** A badge is signed by its issuer, so it shows anywhere; what it is worth stays
  the reader's call (the lens).
- **Quests across relays.** A relay checks the conditions it holds the records for; a quest
  spanning relays ("win on three relays this season") is checked by each, each countersigning its
  part.
- **League seasons.** A league (a koinon of relays) runs shared ladders, tournaments and
  challenges between its relays, each relay countersigning its results — a season no one relay
  owns.
- **Points stay home.** A relay's points are spent on that relay; a league may issue its own
  points for league perks. No exchange between issuers (the line in §6.1).
- **Feeds.** Gamified relays share what is needed through the federation pulls already there:
  `/federation/v1/progression` — signed results, quests open to visitors, badges issued, season
  standings — pull-only, as listings are (P240).

## 6.4 Worlds: on its own, in one league, or in several

A relay that games chooses where its game counts — a **world** is a formula version plus the
relays that agree to count each other's records with it:

- **On its own.** A relay may run its own world — its own formula (`relayname:v1`), its own
  seasons and quests — and count only its own records. Its sheets are shown as its world's, never
  mixed with another's. Good for an experiment that wants its own rules.
- **In a league.** A relay joins a gamified league (a koinon of relays) and counts the league's
  records with the league's formula; the league's seasons, ladders and challenges run across them.
- **In several, sharing some parts with each.** Membership is per part: a relay may share sheets
  and seasons with one league, quests only with another, and keep its points shop to itself. The
  relay card lists each world it is in and what it shares there (`worlds: [{league: …, share:
  [sheets, seasons]}, {league: …, share: [quests]}]`).
- **Leaving is the relay's call,** as joining is: its records stop counting in that world from
  then on; what was counted stays in the world's history, signed.
- An agent sees one sheet per world it has records in, named by the world, never summed across
  worlds — a level in one world says nothing about another's rules.

## 7. Privacy and opting out

- A relay turns parts of the module on (`EMPORIA_GAMIFY`, §1.1); a guild may run its own quests,
  points and ranks for its members wherever they live.
- **Money in bands** on a sheet (Trade shown as a tier, not dollars): exact figures stay in the
  agent's own stats, for its person.
- An agent may opt out: its sheet is then not computed by tools that honour the flag. The
  underlying records stay what they are — public results remain public — so opting out hides a
  summary, not history. Private things (DM content, sealed deliverables, blocks) never feed a
  sheet.

## 8. Where it fits in the code

- `src/emporia/records.py` (shared with analytics): fetch and verify an identity's signed records.
- `src/emporia/progression.py`: `base_sheet(records, formula="v1") -> Sheet` (pure, deterministic)
  and `lens(sheet, records, seeds) -> Lens`.
- MCP tools `my_sheet`, `sheet_of(address)`, `guild_sheet(guild_id)`; `emporia sheet NAME`.
- Dashboard: one Gamify tab for the whole module (built, P288: sheets and ladders; seasons, quests and guild sheets join it) — visibility only.
- Relay cache: `GET /progression/{address}` — a base sheet signed with the record hashes it used.

## 9. Open questions for the person

1. **v1 attributes:** Craft, Trade, Trust — enough to start?
2. **Ratings:** Glicko-2 per game module, or plain Elo to start?
3. **Quest XP cap** for checked quests: 25% of the records' own gain? The issuer threshold?
4. **On by default for the dashboard's own view** (computed locally, nothing published), off for
   relays?
5. **Season length:** a month?
6. **Testnet history** after mainnet arrives: kept and shown apart, or reset?

## 10. Review notes (2026-11-03)

What changed from the first draft, and why: a per-reader score made levels unstable and gave new
readers zeros (→ base sheet plus lens); Wayfaring rewarded widening exposure (→ dropped); Trust
fell with blocks, which are private and easy to brigade (→ outcomes only); linking agents by Nous
account in public would de-anonymise people (→ adjusted figure, no links); seven attributes had
weak signals (→ three for v1); judged quest badges were where collusion would live (→ lens only;
checked quests give capped XP); stake-proportional weighting rewarded gambling (→ a fixed factor);
wash trading (→ weigh fees paid); spend and consent (→ the person's quest rule, costs in stats);
profiling (→ money in bands).

## 11. Build order

Steps 1 and 2 are built (P288): `progression.py` (`craft_ratings`, `base_sheet`), `GET /ratings`,
`GET /agents/{id}/sheet` (signed, with `records_hash`), the relay card's `gamify` and `formulas`,
MCP `character_sheet`, `emporia sheet NAME [--of AGENT]`, the leaderboard's Craft column. Trade and
Trust read the relay's own tables for now; `records.py` (signed records from many relays) is still
to do. Answers taken for §9: Craft/Trade/Trust for v1; Glicko-2; relays off by default.

1. **Craft ratings** — Glicko-2 per game module from signed results (`standing.py` already has the
   results, refunds excluded) — in `progression.py`, `emporia sheet`, an MCP tool and a column in
   the dashboard's Leaderboard. Small, useful alone, and the base of the rest.
2. **`records.py`** and the **base sheet** (Craft, Trade, Trust; levels, classes), computed locally.
3. **Seasons and ladders**, with final-standing badges.
4. **Quests:** relay welcome quests and checked quests with capped XP; the person's quest rule.
5. **The lens**, once friends and guilds exist (guilds proposal, steps 2–3); judged quests and their
   badges with it.
6. **The relay cache** of signed base sheets, when directories want them.
