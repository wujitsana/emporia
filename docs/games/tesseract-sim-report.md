# Tesseract: T0 simulation report

Step T0 (`tesseract-proto`) of the Tesseract plan. Game id `emporia:tesseract:v1`. Not registered; no relay, no dashboard.

- Core: `src/emporia/modules/tesseract_core.py` (pure, stdlib, whole numbers, no float or true division; a test checks the source).
- Tests: `tests/test_tesseract_core.py` (27).
- Simulation: `scripts/sim_tesseract.py` (bots; at most 2 worker processes). Raw output of this report's runs is reproducible with the commands in the last section.
- All numbers are *measured* here, with seeded games of scripted bots. No model has played. Totals are `points // 2` (basis points of movement toward the own target); for the squared score, `points // 20000`.

## 1. Rules as built

Board: the 32 edges of a four-dimensional cube. Corners are subsets of W, X, Y, Z. The pulse enters at `WXYZ`; 4 inputs (three letters), 6 hubs (two letters), 4 outputs (one letter), the empty corner is the drain. **24 channels** (input-to-hub 12, hub-to-output 12), ids like `XYZ>XY`, `XY>X`, in a fixed order (see the module docstring). Widths are whole numbers 0..20.

- **Chance** (`Stream`, SHA-256, unbiased rejection draws): opening widths uniform 1..20, redrawn until the opening mix has every output in 1500..3500 bp (256 attempts, then the last draw stands); 16 pulses per epoch, each input 0..9; each channel dark with chance 1/24 per epoch. Measured: the accepted attempt averages under 2 (0 = first) and is always found well before 256.
- **Targets**: 726 whole-percent mixes, each part 10..40, each exactly 40 points (L1) from 25/25/25/25. `deal_target(draw)` = `targets()[int(draw) % 726]`.
- **Move**: `{"push": {channel: whole number}}`, at most 4 channels, `sum(|push|) <= 8`. No `back`. Zero entries dropped; booleans refused.
- **Epoch**: pushes from all players are summed per channel then clamped to 0..20; forward pass `hub = sum(width * input)` over its 2 channels, `output = sum(width * hub)` over its 3 (dark channel = width 0); per pulse share `625 * out_k // sum(out)` (156 each if all zero); the **mix** is the sum over 16 pulses.
- **Points** (D1, accepted): `first + last`. `first` = distance drop of the player's own pushes added to the old network alone; `last` = distance drop of its pushes added after everyone else's (leave-one-out). Distance = sum over outputs of |mix - 100 * target|. Empty push = 0; points can be negative. Total over 5 epochs; highest wins, equal totals tie. `table_index = 1000 * n * max(total, 0) // sum of max(total, 0)`.
- **Published after each epoch (D2, accepted)**: new widths, pulses, dark channels, mix, the pull table for the next epoch **and who pushed what, at once**. Not published: targets, points. See section 6 for the changes this means for the plan.
- **Experiment switches in `Rules`** (defaults are the plan's rules): `score_power` (1 or 2 = squared gaps), `unit_cap` (most units on one channel; 0 = none), `width_cap`/`open_hi`, `score_mode` (`first_last`, `last`, `min`).

## 2. Pass criteria: my numbers next to the plan's (prototype) numbers

Same rules, same bot code in spirit; chance streams are relabelled (`pulse/`, `dark/`) so individual games differ, only the statistics compare. 200 games per mixed cell, 150 per identical-bot cell, 100 per pair/odd-bot cell.

| # | criterion | this build | plan (prototype) | verdict |
|---|---|---|---|---|
| 1 | skill share (eta^2) at 3 / 4 / 5 / 6 seats, mixed tables | **61.8 / 62.7 / 69.3 / 72.0 %** | 63.7 / 62.0 / 68.9 / 73.1 % | pass, reproduced. (Repeat batteries of 100-250 games gave 57-62 at 4 seats: noise about +-4.) Among competent bots only (lite and greedy, two each) 3.9 % (plan 3 %) |
| 2 | planner vs random | greedy 3540-3791 vs random -91 to -193 per table; difference about +3700 bp; random won 0.005 or less | about +3800 bp | pass, reproduced |
| 3 | no herding ties among identical planners | 6 of 1200 games tied (smart 0/0/1/3, greedy 0/1/0/1 of 150 at 3/4/5/6 seats); mixed tables 0 of 800 | 8 of 1200; 0 of 800 | pass; ties are twins with targets on the same side of the mix |
| 4 | free-rider <= random | idle scores exactly 0; random -63 to -181 among planners; neither wins | idle 0, random -36 to -96 | pass in substance, fail by the letter (idle's mean is above random's) |
| 5 | copycat <= best honest | copy_agg -231 (+-133), copy_player (owners public at once) -412 (+-363), no win in 100 games, vs greedy 3950-4100 | -288, -454, -554 | pass |
| 6 | pair <= 1.1x two solo seats at 5-6 seats | pair_talk 1.04x / 1.01x; pair_joint **1.09x** (+666 +-326) at 5 and 1.07x at 6 seats; at 3 seats pair_joint 1.04x; wrestle 0.56-0.58x, feed 0.43x. Repeat at 250 games: pair_joint 1.07x (+506 +-202) | pair_talk 1.00x, pair_joint 1.05x / 1.04-1.05x | pass but **at the edge**: pair_joint is 1.05-1.09x across all my runs (about 1.07 on average) vs 1.05 in the plan; always inside 1.1 only by about one interval |
| 7 | seat fairness (chi-square, all-random tables) | 3.1 (3 dof), 3.0 (4), 1.4 (5) at 4 / 5 / 6 seats; 2.3 (2 dof) at 3 seats; identical planners 1.4-4.7; one battery cell 12.1 on 4 dof (5 seats, 750 games), the other 13 fairness cells fine | 0.2-4.0 / 0.5-8.8 | pass; the 12.1 (p about 0.02) is one of about 20 fairness tests run, expected by chance, nothing in the rules reads the seat |
| 8 | planner clearly beats random pushes | greedy 3500-3800 vs random -90 to -190, random won 0 | 3551 vs -186 | pass |

Table size (identical bots): greedy 4179 / 3973 / 3750 / 3515 and smart 3413 / 3297 / 3075 / 3033 at 3 / 4 / 5 / 6 seats (plan: 3945 / 4185 / 3824 / 3509 and 3452 / 3174 / 3139 / 3060): about 16 % lower at 6 seats than 3, so raw totals do not compare across table sizes. Table index mean 1000 and sd 270-355 at each size. Luck (5 greedy): mean 3827, sd 1308, sd of table means 930, within a table 920 (plan 3825 / 917 / 975); final distance to the own target 3992 (start about 4000): nobody "arrives".

Reproduced: yes. Every number is within the sampling interval (+-180 to +-270) of the plan's, except pair_joint (1.07-1.09x against 1.04-1.05x, within one interval but consistently above).

## 3. The depth levers (risk R1)

Measured by `--only levers`: 100 games per cell, same seeds across cells (paired), one odd bot among 4 `greedy` (a one-step-at-a-time planner using four nominal pulses). `greedy1` is a deeper planner (one unit at a time). `oneshot` puts as many units as the rules allow on the single best channel. `lite` reads the pull table: top 2 channels by first-order gain, 4 units each. `lite_thin` is a crude reader (see caveat). Margins are greedy minus the other bot, as a percent of greedy's total, with the paired interval.

| rule set | skill share 3 / 4 / 5 / 6 | greedy over oneshot | greedy over lite | greedy over lite_thin | greedy1 over greedy | spoiler cost to others | pair_joint x (5 seats) | pair_talk x | seat chi2 4 / 5 |
|---|---|---|---|---|---|---|---|---|---|
| base | 62.4 / 57.5 / 69.4 / 71.0 | -2.3 % (+-7) | **+9.7 %** (+-7) | +15.1 % | +1.2 % | 6.7 % | 1.09 | 1.04 | 2.3 / 3.7 |
| 1 squared gaps | 55.3 / **48.6** / 59.9 / 61.5 | +0.9 % | +3.0 % | +13.5 % | +1.9 % | 5.5 % | 1.08 | 1.03 | 0.6 / 6.0 |
| 2 at most 4 on a channel | 68.6 / 54.8 / 71.5 / 71.7 | **+43.4 %** | +9.8 % | +13.7 % | -0.6 % | 6.1 % | 1.06 | 1.03 | 3.1 / 4.8 |
| 3 widths 0..12 | 63.9 / 64.7 / 67.3 / 72.6 | +9.1 % | **+16.9 %** | +22.3 % | -2.0 % | 5.7 % | 1.04 | 0.97 | 2.6 / 1.2 |
| 4 thin pull table | not a rule change a bot can feel (see caveat) | | | | | | | | |
| 1 + 2 | 59.3 / **50.4** / 61.4 / 65.0 | +33.5 % | -4.9 % | +8.4 % | +5.7 % | 7.8 % | 1.09 | 1.04 | 1.6 / 4.1 |
| 1 + 3 | 54.0 / 52.9 / 55.4 / 60.8 | +8.8 % | +12.0 % | +20.7 % | -1.4 % | 5.1 % | 1.10 | 1.01 | 1.7 / 3.2 |
| 2 + 3 | 62.6 / 58.7 / 66.2 / 72.9 | **+43.8 %** | +10.4 % | +16.3 % | +2.2 % | 6.5 % | 1.06 | 1.01 | 6.1 / 1.0 |
| 1 + 2 + 3 | 51.3 / **47.1** / 56.8 / 58.1 | +36.9 % | +3.4 % | +12.1 % | -0.1 % | 7.4 % | 1.09 | 1.10 | 4.2 / 3.8 |

(Margin columns are greedy minus the bot. The greedy1 column is greedy1 minus greedy: positive means the deeper search helps.) Interval on a margin is about +-7 percentage points at 100 games.

Confirmation at 250 games (base, widths 0..12, and 2 + 3):

| rule set | skill share 3 / 4 / 5 / 6 | greedy over oneshot | over lite | over lite_thin | spoiler cost | pair_joint x | pair_talk x | seat chi2 4 / 5 |
|---|---|---|---|---|---|---|---|---|
| base | 60.5 / 59.7 / 67.1 / 71.3 | +2.0 % | +14.6 % (+-4) | +17.5 % | 5.0 % | 1.07 | 1.02 | 5.3 / 12.1 |
| widths 0..12 | 59.4 / 62.4 / 66.5 / 69.9 | +7.6 % | **+18.7 %** (+-4) | +22.7 % | 6.7 % | 1.05 | 0.98 | 1.4 / 4.9 |
| 4 per channel + widths 0..12 | 60.3 / 59.2 / 65.5 / 70.1 | **+44.7 %** | +15.0 % (+-4) | +17.1 % | 7.5 % | 1.06 | 1.03 | 0.9 / 0.8 |

Other criteria in every row above also hold (copycats, idle, random: no wins, each strongly negative or zero; no ties beyond 1 game in 400).

### What the levers did

- **No lever fixes depth.** In every rule set a search planner beats a player who reads two table rows by 3-19 %, and a one-unit-at-a-time search (`greedy1`) is never better than the four-unit search (all differences inside +-6 %, no sign). More planning does not pay; the game rewards aiming (criteria 1, 2, 8), not deep planning.
- **At most 4 on a channel** removes the literal trivial strategy (all-in on one channel loses 43-45 % and never wins), but only because it forces the second 4 units elsewhere: the replacement, read two rows, push 4 each, is `lite` and sits at 85-90 % of the planner, same as before. It fixes the letter of "all 8 on the top channel", not the substance.
- **Widths 0..12** is the only lever that widens planner over reader (lite margin 14.6 % to 18.7 % at 250 games, lite_thin 17.5 % to 22.7 %, oneshot margin 2 % to 8 %): channels fill up and crowding matters. The gain is real but small and its interval touches the base's.
- **Squared gaps** hurts: skill share drops 7-10 points (4 seats 48-50 %, which fails the 50 % criterion) and it narrows the margin over lite to 3 % (it rewards closing a big gap, which any reader does). It does remove twin ties (0 in these runs vs 1) but not worth it. Rejected.
- **Spoiler cost to the others** is 5-8 % in every rule set (paired against an idle seat; the plan's 2 % came from unpaired cells). No lever moves it beyond noise (+-4). Players still meet only through the board.
- **Pairs**: pair_joint 1.04-1.10x everywhere, never clearly above 1.1x; widths 0..12 and 2 + 3 sit lowest (1.04-1.06x).
- **Seat fairness, copycat, free-rider** unchanged and passing in all rule sets (one stray chi-square, see above).
- **Caveat on lever 4 (thin pull table).** It changes what a model is shown (+1 rows only, no `you.best`), not the rules, and a scripted bot computes its own gains, so bots cannot measure it. `lite_thin` (reader that looks only at the largest gap) stands in for a weaker reader and trails greedy by 14-23 %, but that is an assumption about reading behaviour, not a result. It can only be tested with models (R4).

## 4. Recommended rule set

**Widths 0..12 and at most 4 units on one channel** (everything else as built). Reasons: passes every criterion (skill share 59-70 % at 3-6 seats at 250 games, pairs 1.06x, copycat, free-rider, fairness); the all-in play is removed outright (-45 %, wins none of 100); widths 0..12 gives the (small) best planner-over-reader margin; totals scale up by about a third, which changes nothing. If only one lever is wanted: widths 0..12 (best margin, no new validation rule). Do not use squared gaps.

Plainly: this does not solve "the best play is simple". A reader of the pull table plays at about 85 % of a planner under the recommended rules (85 % base). It is a rule set in which aiming is rewarded and the cheapest degenerate strategy is closed, not a deep game. Whether models do better than `lite` is the question for R4.

The recommended set is not the default in the core (the defaults are the plan's rules, as the plan's section 2.2 specifies); T1 should take `width_cap=12, open_hi=12, unit_cap=4` once the user agrees.

## 5. Open risks

1. Depth (above): the strongest bot gains little over a reader; all search depth levers measured give no better than about 19 %.
2. Interaction is weak (5-8 % spoiler cost); the social part (table talk, guessing targets) is unmeasured.
3. Pair gain sits at 1.05-1.09x against the 1.1x limit; a smarter coordinating brain than `pair_joint` (three-player coalitions, repeated play) is not simulated.
4. Free-rider criterion holds only in substance (idle 0 vs random negative).
5. No model has played; `lite` is an upper-bound guess for a reading model, and the thin-table lever (4) is untestable by bots.
6. Not simulated: sit-out and non-open paths, three-player coalitions, 3 and 6 epoch games and the dark/budget/epoch sweeps with the final score (the `sweep` experiment exists, not run for this report).
7. Relay-held-moves trust issue is gone with D2 (see below); the cost is that copying becomes possible one epoch faster, measured above: `copy_player` -412 and -517, never wins.
8. Seat-fairness chi-square of 12.1 on 4 dof in one cell: probably chance; watch it in T1 through-the-relay tests.

## 6. Changes to the plan's text from decision D2 (who pushed what public at once)

Not made in the plan file; this is what to change there.

- **1.1** last sentences: "After each round everyone sees the new network and mix **and who pushed what**; targets and scores are shown at the end".
- **1.5 step 2** (previous epoch's moves published when everyone has sealed) becomes: owners published with the epoch result. **Step 6** "Published at once" adds: the pushes with their owners; "Not published" drops "who pushed what".
- **1.7**: "Sealing and not opening" loses the argument about hiding a bad move from the others: it still holds (a sealed move that is never opened is empty for everyone's score, the player placed last).
- **1.8**: the table row "who pushed what in epoch e" becomes "after each epoch resolves"; delete the paragraph about the relay holding opened moves, the dishonest-relay leak and the fallback; there is nothing left to withhold.
- **2.2**: `EpochPublic` gains `pushes: {player: {channel: amount}}` (built: field `pushes`).
- **2.3**: delete the `served(...)` SDK hook entirely ("no new SDK hook"); `module_sdk.py` gets only `ONE_SEAT_PER_OPERATOR`.
- **3.1**: `copy_now` is merged into `copy_player` (both see the previous epoch's owners; there is no late variant anymore).
- **4 (agent view)**: `owners` is now **the previous epoch, published at once** (`"owners": {"epoch": 2, "pushes": {...}}` when the agent decides in epoch 3, as before but no longer "one epoch behind" the network; the agent also sees the current epoch's net `moved` per channel, which is the sum of those pushes). Delete the sentence "owners is one epoch behind (1.8)". Under D2 the agent can compute exactly what each other player did to the network; the copycat numbers above show copying still does not pay.
- **6**: D2 and R7 are closed; the fallback row and "relay-held moves" risk disappear.

Public-state function: `play_epoch(...) -> EpochPublic(epoch, pulses, dark, widths_before, widths_after, mix, pushes, pull)`; targets and points are never in it (`epoch_points` is separate and takes the targets).

## 7. Reproduce

```
export EMPORIA_NO_DOTENV=1 PYTHONPATH=src
python scripts/sim_tesseract.py --only tourn --games 200 --workers 2
python scripts/sim_tesseract.py --only skill --games 100
python scripts/sim_tesseract.py --only same --games 150
python scripts/sim_tesseract.py --only degenerate --games 100
python scripts/sim_tesseract.py --only coalition --games 100 --ns 5,6,3
python scripts/sim_tesseract.py --only luck --games 200
python scripts/sim_tesseract.py --only levers --games 100 --strong
python scripts/sim_tesseract.py --only levers --configs base,w12,cap4+w12 --games 250
```

All runs used at most two worker processes (the script caps `--workers` at 2). The whole set takes about 15 minutes. A sample (1 in 25) of the games is replayed through `play_game` and checked against the live totals.
