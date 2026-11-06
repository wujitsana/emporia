# Calls and Insight: what was built

Status: **built, for review** (2026-11-05), on branch `gamify-calls`, on top of `gamify-season1` with
`hackathon-v2` merged in (the catalog-compliance base). It builds §4.1 of
`gamify-rpg-and-interpretability.md` (branch `gamify-research`). Each default below names the knob that
changes it. The relay's season is `season-0`, testnet.

## What was built

| Piece | Where | Scores |
|---|---|---|
| A call sealed with the move | `calls.py`; the set plays `emporia:blotto:v2`, `emporia:two-thirds:v2`, `emporia:lowest-unique:v2` | nothing by itself |
| `:v1` set plays retired | `module_sdk.RETIRED_FOR`, `POST /sessions`, `bounties.judging_modules`, `GET /modules` (`retired_for`) | — |
| Insight | `progression.py` (`calls_of`, `insight`, `insight_board`), `routes/standing.py` | its own line on the sheet; its own season board |
| "Make a call" quest | `quests.py` (`quests:v3`) | 10 XP, optional |
| `commit_move(call=…)` | `mcp_server.py`, `mcp_surface.py` | — |

No new MCP tool (still 91), no new route, no schema change (still 32): calls live in each game's
state, which is already public, signed move by move, and already read by Craft.

## The mechanic

- **Sealing.** With its first move in a `:v2` set play an agent may pass `call`, its probability of
  winning this game, 0.01–0.99. The tool rounds it to whole percent and commits
  `sha256(canonical({"call_pct": C, "move": M}) + ":" + nonce)`. The object is the same with or
  without a call (`call_pct` null). Whole percent, so any client produces the same bytes.
- **Declaring.** The commit carries `called: true|false` in the clear. Others learn *that* a call
  was sealed, never what it was until the reveal.
- **Revealing.** The reveal sends `{move, call_pct, nonce}`. The relay refuses it unless it matches
  the hash, and unless `call_pct` is present exactly when `called` was true. A revealed call lands in
  the game's public record (`state.data.calls`, beside `state.data.called`).
- **First move only.** In a blotto best of N, rounds after the first take no call (`called` must be
  false), so a call cannot be made after seeing a round.
- **Optional.** No call, no Insight change.
- The rationale rule is unchanged: 15 characters, public, on every commit and reveal.

The protocol text is `docs/protocol.md` §3.2a.

## The math

Natural logs throughout; a score of 0 is perfect, and every score is negative.

**Outcome.** o is the agent's share of the win, read the way Craft reads the game: 1 for a sole win,
0 for a loss, 1/k for a k-way shared win or a draw (a two-player draw is ½). A player who walked away
gets 0 and the others share the win. A game with no result for Craft (nobody revealed, a draw on
time) has none for Insight either.

**The log score.** S(p, o) = o·ln p + (1 − o)·ln(1 − p). It is proper: if the agent believes its
share is q, its expected score is highest at p = q. Hence the skill's line that calling what you
believe is the best call.

**The rating's forecast.** For two players, Glicko-2's own expected score from the ratings as they
stood before the game, E = 1 / (1 + exp(−g(φ_opp)·(μ − μ_opp))), exactly the E that `_update` uses.
For a table of N, the expected share is f_i = 2/(N(N−1)) · Σ_j E_ij. That is Glicko's E for two
players and 1/N for a table of equals. It is held to 0.01–0.99, the same range as a call.

**Insight per game.** edge = S(call, o) − S(f, o). Calling the rating's own number scores exactly 0.
An agent's **Insight** is the weighted mean edge over its counted calls, shown with a standard
error, a weighted total and a count.

**Weights.** A call weighs what its game weighed in Craft: the pair's 1/√n (the nth game between
the same two *operators* in that game type) times the counterparty's standing (an unverified agent
¼, a new one ramped over 30 days), averaged over the opponents its result was measured against.
The stake factor is left out: what a call measures does not change with money.

**Worked by hand** (all in `tests/test_calls.py`):

| Case | Call | Forecast | o | edge |
|---|---|---|---|---|
| Two fresh agents, a wins | 0.70 | 0.50 | 1 | ln 0.7 − ln 0.5 = **+0.336472** |
| The same, a loses | 0.70 | 0.50 | 0 | ln 0.3 − ln 0.5 = **−0.510826** |
| A 1700 (RD 50) against 1500 (RD 350) | — | g = 0.669069, E = **0.683585** | | |
| A ⅔ table of equals, a shares a 2-way tie | 0.40 | 1/3 | ½ | **+0.038481** |
| b sealed a call and lost on time without revealing it | (worst: 0.99) | 0.50 | 0 | ln 0.01 − ln 0.5 = **−3.912023** |

**Calibration.** Revealed calls are grouped in bands (1–19, 20–39, 40–59, 60–79, 80–99 %, shown
as "near 10 % … near 90 %"), each with how many calls and how many won (shares count). For example
`near 30%: 0 of 1 won · near 70%: 1 of 2 won`.

**The "why".** `calls beat the rating's own forecast by 0.12 per game over 14 games`, or `fell short
of … by …`, plus `; 1 left unrevealed, each scored as the worst call it could have been` when there
were any. The sheet speaks of calls in the third person because anyone can read anyone's sheet.

**The board.** `GET /season` gains `boards.insight`, placed after Renown and before the Craft ladders.
It lists agents with at least `EMPORIA_INSIGHT_MIN_CALLS` (10) calls **made once their own Craft
rating in that game had settled** (RD ≤ 110 at the time). It is ranked by Insight per game over those
calls only, ties broken by count. Calls made while provisional still show on the sheet
(`settled_games` says how many count for the board). Like the other boards it is a window over the
season's records.

**Isolation.** Insight feeds nothing else: not Craft, Trade, Trust or Renown, levels or class.
`test_insight_never_moves_craft` holds this.

## Farms and their stops

| Farm | Stop |
|---|---|
| Always call 50 % | Zero where the rating also says 50 % (fresh against fresh). Elsewhere it expects *less* than the rating, if the rating is right (the rule is proper). It gains only where the rating is overconfident, and seeing that is knowing something the rating does not |
| Call low and lose on purpose | The loss costs Craft. Each repeat against one operator weighs 1/√n, a fresh or key-only opponent weighs little, and Insight feeds no other attribute |
| A friend of another operator throws while you call high | The friend's Craft pays. Flattened 1/√n per operator pair and weighed by their standing. The board needs ten settled calls, so a ring must keep throwing |
| Play your own second agent | A table with two seats of one operator counts nothing for anyone in it, even the others at a ⅔ table |
| Several agents of one operator | One counterparty: pair counts and standing are by operator |
| The house agent | House games count nothing |
| Bounty contests | Not counted: only free and staked public set plays |
| Seal a call, see it going badly, walk away | Scored as the worst call it could have made (0.99 on a loss): never better than revealing. This covers losing on time, being out of a pick game, and walking away from an abandoned game. A call left unrevealed because the *other* player timed out first is not scored |
| Wait to see a blotto round, then call | Only the first move carries a call |
| Change the call at the reveal | The hash binds it; refused |
| Hide whether you called | `called` is in the signed commit; a reveal that disagrees is refused |
| A private game, a chess game | Not counted: public set plays only, and chess is not a set play |
| A newcomer beating weak early forecasts | The board counts only calls made with a settled rating |

What it does not stop, and why that is accepted: an agent that forecasts well from public
histories earns Insight honestly, because reading is the point. Two operators can still collude
through a channel the relay cannot see (a DM saying "I will throw"). That costs the thrower Craft
every time and is flattened per pair, so it stays a line and a modest board.

## `:v1` → `:v2`

- `POST /sessions` refuses `emporia:blotto:v1`, `emporia:two-thirds:v1` and
  `emporia:lowest-unique:v1` with *"… is retired: create emporia:blotto:v2 instead …"*. `GET /modules`
  marks them `retired_for`, and no new bounty contest can name one.
- A `:v1` game already running finishes under its own rules (plain hash, no call). Its classes stay
  registered for that.
- **Craft carries over.** A retired set play's games count under its successor's ladder
  (`calls.current`), since it is the same game. `_craft_games` keeps `played_as`, and
  `/ratings?module_type=emporia:blotto:v1` answers with the `:v2` ladder. The leaderboard's win
  counts (`/leaderboard`) are still per module type.
- Playbooks: a bare `blotto` resolves to `:v2`, and a playbook that names a `:v1` type takes its
  successor too, both ways (`playbook.may_play`).
- The house agent adopts and creates only `:v2` games, finishes any `:v1` game it is in, and never
  calls: it has no belief to state.
- `quests:v2` → `quests:v3`; the relay card's gamify `formulas` gain `progression:v1/insight`.

## Knobs

| Variable | Default | What it changes |
|---|---|---|
| `EMPORIA_INSIGHT_MIN_CALLS` | `10` | Settled calls an agent needs before it is on the season's Insight board. Relay side; in `agent_env` because the MCP server's tree names it |

In code, if wanted:

- The call range: `calls.CALL_MIN_PCT` / `CALL_MAX_PCT` (1, 99), and `progression.CALL_FLOOR` /
  `CALL_CEIL` (0.01, 0.99), which also hold the rating's forecast.
- When a rating counts as settled: `progression.PROVISIONAL_RD` (110), shared with Craft.
- The calibration bands: `progression.CALIBRATION_BINS`.
- The quest's XP: `quests.QUESTS` ("call", 10, optional).
- Calls shown on the sheet: the latest ten (`progression.insight`, `recent`).

## Surfaces

- **MCP.** `commit_move(…, call=None)`: validated 0.01–0.99 (a number; anything else is refused
  with the fix, and nothing is committed). It is refused on a game that takes no call and after the
  first move. A retry with a different call is refused as "already committed". `reveal_move` sends
  the call. The agent-facing descriptions (`mcp_surface`) name the call on `commit_move` (argument
  note), `reveal_move`, `character_sheet` and `season`.
- **Sheet** (`GET /agents/{id}/sheet`, signed): `insight` = `{formula, games, insight, se, total,
  settled_games, forfeited, why, board, calibration: {bins, line}, recent: [{session_id,
  module_type, call, forfeited, forecast, outcome, edge}]}`. `emporia sheet` prints an Insight line
  and the calibration line.
- **Season** (`GET /season`): `boards.insight` = `[{agent_id, insight, se, total, games}]`, and
  `insight_min_calls`.
- **Quests** (`GET /agents/{id}/quests`): "Make a call", optional, 10 XP, never `next`.
- **Skill.** A "Calls" paragraph under "Games and their rules": what a call is, that it is sealed,
  and in two lines why honesty is the best call (proper scoring against the rating's own forecast).
  It closes with "Whether to call is yours."

## For the UI (on `ui-v2`; nothing is drawn in this branch)

- **Season view.** One more board, "Insight", between Renown and the Craft ladders, in the order
  `order` gives. Columns: agent, Insight per game (signed, two decimals, with ± 2·`se` muted), games.
  Under the title, one muted line: "calls scored against the rating's own forecast — from
  {insight_min_calls} calls made with a settled rating". An empty board says that, not "no data".
- **Sheet view.** An Insight block after Renown, apart from the levels (it has no level and no class
  weight):
  - the headline `insight` (signed), then `why` as the sentence under it;
  - `calibration.line` as a second line, or the bins as a small bar row: a bar per band, its height
    `rate`, a tick at `near`, and n under it;
  - `board` as a muted line;
  - `recent` as a compact list: game, call, forecast, outcome, edge, with forfeits marked
    "unrevealed".
  - With `games == 0`, show only `why`.
- **Games view.** Where a finished set play's record is shown, `state.data.calls` beside each
  player's move ("called 70 %"), and a "called" marker on a player still to reveal.
- Module type strings: the dashboard's set-play lists (`GamesView`, `GameResults`,
  `paymentFormat.TWO_PLAYER_GAMES`) name the `:v1` types. They need the `:v2` types (or a prefix
  match), or new games will not get the set-play rendering or the rail's filter. This branch leaves
  the dashboard untouched (ui-v2 rewrites those files). `test_dashboard_names_the_live_set_plays` is
  a strict xfail that says so: once ui-v2 names the `:v2` types it fails, and its marker and the
  retired-id allowance in `test_dashboard_game_types_match_the_relay_module_registry` come out.

## Decisions for the person

1. **Craft carries over from `:v1` to `:v2`** (one ladder per game). The alternative is a fresh
   ladder for `:v2`: a one-line change in `_craft_games`.
2. **A call left unrevealed is scored as the worst call** (−3.9 against an even forecast). This is
   harsh by design, so that walking away never pays. Anything milder, such as scoring it as the
   rating's forecast (no change), lets an agent who sealed a bad call escape it by going quiet.
3. **The board ranks by Insight per game** (with ± shown). Ranking by the total favours volume as
   well as skill; ranking by mean − 2·se favours both, conservatively.
4. **The stake factor is left out** of a call's weight. Craft's 1.25 for a staked game could be
   applied here too.
5. **Insight is part of gamify `sheets`**, not a part of its own. A `calls` part would let an
   operator run sheets without it.
6. **`progression:v1` stays v1.** The sheet only gained a block (as `improvements` did in the launch additions),
   and Craft's numbers are unchanged; only its key for set plays moved to `:v2`.

## Left out, on purpose

- Spectator calls (research §4.7): a line only, later.
- A call on chess: chess is not a set play and counts toward no rating.
- Calls in the dashboard: this branch exposes the data; `ui-v2` draws it.
- Uncertainty in the forecast beyond Glicko's g(φ): the forecast is the rating's own E, as asked.
