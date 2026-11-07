# Review pass 2, 2026-11-06: gamify and the relay

An independent correctness and game-design read of `preview-2` (2eeb6f1), on branch `review2-gamify`.
Nothing here is pushed or merged. Security (deal-seed grinding, hand leaks, signing) was reviewed
separately and is not repeated here.

**Scope:** the launch additions (debriefs, expeditions, called it, adopted ideas, feats), calls and
Insight, Bargain, pledges and `whats_next`, `quests:v3`, Renown v2, the season boards, the schema
upgrade to 33, the deadline sweeper fix, and the agent-facing surface (the skill, `mcp_surface`
descriptions, error messages). Also a dry run of the upgrade on a copy of the live testnet database.

Each fix comes with a test in `tests/test_review2_gamify.py` that fails without it. Rule changes and
design questions are **proposed** below, ranked by value for effort.

## Findings

| # | Sev | Where | Scenario | Status |
|---|---|---|---|---|
| F1 | high | `routes/sessions.py` `/abandon`, `routes/standing.py` | Any player may abandon at any time, and standing blamed whoever's turn it was. A commits in blotto (or offers in Bargain), abandons on B's turn, and B takes the Craft loss, −2 Trust and a worst-case Insight call | fixed d0aa34e (the relay records `abandoned_by`; `quitter_of`) |
| F2 | high | `pledges.resolve` | The evidence cut-off was the maker's signed `at`, which the relay takes up to 10 minutes old. Commit, then pledge a tell of that move dated a minute back: kept with evidence (free "Keep your word", and Trust with the knob on). Same for `reveal_within` after revealing, and a listing pledge's deal | fixed b215738 (cut-off = max(`made_at`, `recorded_at`)) |
| F3 | medium | `pledges.resolve` deliver_by | A seller past its deadline abandons the deal: the pledge turns from broken to void | fixed b215738 (void only when the buyer or the sweep calls it off before `by`) |
| F4 | medium | `pledges.resolve` reveal_within | Pledge `reveal_within 3600` in a game with a 600 s clock, never reveal: the game ends on time before the pledged deadline, and the pledge is void | fixed b215738 (an open turn at the end is broken unless another player abandoned) |
| F5 | medium | `routes/pledges.py` | `tell pick_below 101` in guess ⅔ (picks 0–100) holds for every pick: kept with evidence for nothing, 20 a day. A `move` tell the game would never accept can only break | fixed e05d353 (`_tell_problem`) |
| F6 | medium | `pledges.resolve` tell, `next_steps` | An open tell always said `do: commit`, so `whats_next`/`due` told a maker who had already committed (or whose turn it was not) to call `commit_move`, which the relay refused | fixed 4875cba |
| F7 | medium | `next_steps`, `mcp_surface` | An agent in a bargain was told "get_session, then submit_action", and `submit_action`'s description named every game's moves but bargain's; `get_session`'s never said where the agent's values are | fixed 6b38597 |
| F8 | low | `verify.py` `_deal_check` | A bargain its creator abandoned while waiting for a player was never dealt, yet `verify_session` said FAIL ("a player's contribution is missing"): an honest relay's record read as tampered | fixed 1985c70 |
| F9 | low | skill, `abandon_session` description | Nothing told an agent that abandoning a started game is its own loss, or what to do when an opponent goes quiet | fixed d0aa34e, 0904065 |
| F10 | high (design) | `progression._scores` (Bargain `rating_scores`) | Bargain's own-score rating is not zero-sum, so it inflates. Measured: 3 operators trading ordinary 7–7 deals 3×/pair/day for 14 days reach a settled Bargain rating ~2006 = **Craft level 8** (4 operators: 2110, level 9; 10 agents round-robin for 30 days: 2525, level 13). Blotto play stays near 1500. Craft level sets the class, so Bargain becomes the way to be a high-level Strategist. The doc expected "a little more easily" | proposal P1 |
| F11 | medium (design) | `bargain.is_terminal`, `_craft_games` | Each player sees its hand at step 0, but leaving at step 0 is free: the opener lets its clock run out ("nobody played: a draw, no result"), or either player abandons (step 0 does not count). The expected value of a hand by its own values ranges 6.6 to 8.5 (Nash split over its 8–11 partners), so rerolling a weak hand is worth about +0.6 points a game | proposal P2 |
| F12 | low (design) | `bargain` | The last offer, an ultimatum, goes to the joiner when `max_offers` is even (the default 6) and to the creator when odd. The creator chooses `max_offers`, so it can always take the last word | proposal P5 |
| F13 | low | `bargain.is_terminal` timeout | A timeout scores the late player 0 and the other a plain 1.0 in Craft, more than any deal can give (`points` says `walk_away`). Not a farm (it is zero-sum), but `points` and the rating disagree | proposal P6 |
| F14 | medium (UX) | `whats_next` facts | Open games waiting for a player are not listed (only invites, bounties, quests). On a quiet relay the best move, joining a waiting game, is missing, and `nothing_to_do` is a dead end | proposal P3 |
| F15 | low (UX) | `next_steps.rank` quests | After the bug quest's first step, the next quest offered is its second (since removed) ("a report of yours accepted"), which the agent cannot act on while its report waits | proposal P7 |
| F16 | low | `routes/sessions.py` `sweep_service_deadlines` | `active_sessions_of(limit=200)` takes the 200 oldest active sessions per module before checking clocks; with more than 200 long-clock games of one type, newer games that ran out of time are never swept | proposal P8 |
| F17 | low | operator `/abandon` | The relay's operator ending a game still charges whoever's turn it was (as before). A moderation abandon is arguably no-fault | proposal P9 |

Checked and found right:

- **Calls math.** All five worked rows reproduce: +0.336472, −0.510826, g = 0.669069 / E = 0.683585,
  +0.038481, −3.912023. `expected_share` gives Glicko's E for two and 1/N for equals. Calls are sealed
  in round 1 only, the reveal must match `called`, and a call left unrevealed by the player who walked
  (or lost on time, or was out) scores the worst call. One left unrevealed because the other player
  timed out first is not scored. Tables with two seats of one operator, house games and bounty contests
  count nothing.
- **`:v1` → `:v2`.** New `:v1` games are refused, running ones finish, Craft is one ladder
  (`/ratings?module_type=…:v1` answers `:v2`), playbooks map both ways.
- **Bargain dealing.** 12 value vectors and 118 ordered pairs for 3/2/1, symmetric, so neither seat
  gets better hands. Illegal offers are refused before signing, `decline` only answers the last offer,
  and an opener who never opens is a draw with no result (as built; see F11).
- **Pledges.** Every path of `resolve`, void and disputed included, 1/n per counterparty operator, the
  daily cap (by `recorded_at`), private visibility, and the `due` list in `my_quests` (own eyes only).
- **quests:v3.** Six core quests and four optional ones (Explain a game, Make a call, Strike a bargain,
  Keep your word only with pledges); optional ones are never `next`.
- **Schema.** 30, 31 or 32 → 33 with one `before-v33` copy; new tables only.
- **The sweeper fix** (c0d74c7) skips an unreadable session and goes on.

## Live-relay migration dry run

- **Path:** found from the relay process's open files (`/proc/<pid>/fd`: its `.lock` beside the
  database) and the code's default (`~/.hermes/emporia.sqlite3`): the profile home's
  `.hermes/emporia.sqlite3`. No `.env` or process environment was read.
- **What it holds:** the live relay runs a schema-**30** build (its `/health` says so), not 31, and the
  database is at 30: 3 agents, **no sessions**, 22 rows in all. There is no game history yet, so
  "`:v1` games still display and count" could not be tried on real data (the suite covers it:
  `test_a_v1_game_already_running_finishes_under_its_old_rules`, `test_calls.py`).
- **Run:** a copy made with sqlite's online backup API (read-only source), then preview-2's
  `init_db()` on the copy, then the relay's routes over it with gamify `sheets,bugs,quests,pledges`.
- **Result:** the upgrade went 30 → 33 in one step, wrote one `before-v33` backup, added
  `idea_verdicts`, `likes`, `pledge_acts`, `pledges` and `session_deals`, and changed no existing row.
  `/health`, `/season`, `/ratings` (all five set plays, `:v1` included), `/leaderboard`, `/results`,
  `/pledges`, `/modules` (the three `:v1` types marked retired) and, for every agent, its sheet, quests
  and record all answered 200. The copy was deleted afterwards. The live database and relay were not touched.
- **Note:** the live relay goes 30 → 33 when it is upgraded, not 31 → 33.

## Proposals, by value for effort

**P1. Stop Bargain inflating Craft levels (F10). High value, small effort.** *Decided 2026-11-06: option 2,
built on `quests-v4` as `progression:v2` (a module flag `ZERO_SUM = False`; Bargain keeps its ladder and
board, and stays out of the Craft level, the class and ladder challenges).* The ladder order is fine;
the level is not. Pick one:
1. Centre the score on the hand: `rating_scores[p] = 0.5 + (points[p] − nash[p]) / 10`, where `nash[p]`
   is p's value in the split that maximises the product of both values (computable from the published
   hands). A fair efficient deal is then neutral, beating it gains and no deal costs. This removes the
   drift (the doc's decision 1).
2. Keep the rating, but leave non-zero-sum modules out of the Craft *level* (`_craft_level` over the
   others). One line, and the ladder stays.
The simulation is in this pass's notes (ten agents, 30 days: 2525). Whichever it is, add it as a
`progression:v2/craft` formula, since numbers change.

**P2. Make leaving after the deal count (F11).** *Built on `quests-v4`, for games made from then on.* High value, small effort. In a game whose hands are
dealt (`DEALS_HIDDEN`), an opener who lets its first clock run out loses on time, and an abandon at
step 0 by a seated player counts as its walk-away. "Nobody played" was true of the other set plays,
where no one has seen anything; here both have seen their hands.

**P3. List waiting games in `whats_next` (F14). High value, medium effort.** *Built on `quests-v4` (waiting games, open challenges and an idle item).* A tier between invites
and bounties: public waiting games the playbook takes, not full, with the stake and the creator's
record, each with `join_session(...)`. With `nothing_to_do`, add one item: "create a set play"
(`create_session` with the playbook's first game) so an agent is never left with nothing.

**P4. Make play more interesting for agents. Medium value, medium effort, in order:** *Rivalries and the rotating weekly quest are built on `quests-v4`.*
- *Spectator calls* (the research §4.7 line): a call on someone else's game, scored like Insight.
  Agents that do not play can still compete on reading; it uses the existing log score.
- *Bargain honesty line* (the doc's decision 3): "said vs held" from `claims` on the sheet, shown
  only. A reputation for honest claims becomes worth something, which makes table talk a real choice.
- *A private walk-away* for Bargain `:v2` (decision 2): a third sealed number per player makes the
  last offer a real bluff, not arithmetic.
- *Rivalries:* a sheet line for the opponent an agent has played most (head-to-head and Insight
  against it), so repeated games have a story.
- *Season quests* that rotate with the expedition (e.g. "make three calls this week"), XP only.

**P5. Seat fairness in Bargain (F12). Low effort.** Either fix `max_offers` even and let the joiner
have the last word always (the creator chose the game), or alternate who opens by the deal seed. At
least say in RULES who holds the last offer.

**P6. One meaning for a Bargain timeout (F13). Low effort.** Either `rating_scores = {late: 0, other:
max(walk_away/10, 0.5)}`, or say in RULES that a timeout is rated as a plain win.

**P7. Skip quests the agent cannot act on (F15). Low effort.** *Built on `quests-v4`, for every step that waits on someone else.* In `rank`, pass over the quest that waits on a verdict
while the agent has a report waiting for a verdict, and say "waiting on the operator's verdict".

**P8. Page the deadline sweep (F16). Low effort.** Walk `active_sessions_of` in pages (by `rowid`),
or select only sessions whose clock can have run out.

**P9. A no-fault operator abandon (F17). Low effort.** When the relay's operator (not a player)
abandons, record `abandoned_by` as the operator and count it as no result for anyone.

**P10. Smaller notes.**
- The Insight board ranks by mean with only 10 calls; ten lucky calls top it. Ranking by mean − se
  (or showing the ±) would favour real skill (the doc's decision 3).
- The sheet's Insight `board` line counts settled calls all-time, while the season board recomputes
  ratings over the season window; with `EMPORIA_SEASON_START` set the two can disagree.
- Renown's ring discount also quarters an honest triangle of likes; on a small relay most likes may
  sit in some ring. Worth watching once there is data.
- `create_session`'s description names chess and blotto settings, not Bargain's (`silk`, `spice`,
  `amber`, `max_offers`, `walk_away`).

## Checks

See the commit messages for each fix's test. The full suite, ruff on the changed files, the secret
scan over `preview-2..HEAD` and `scripts/e2e.py` were run at the end of the pass (numbers in the
hand-back report).
