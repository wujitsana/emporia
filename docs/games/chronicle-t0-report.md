# Chronicle T0 report: the pure core, its tests and its simulation

Phase T0 of `games-for-agents-plan.md` (section 9). Branch `games/chronicle-t0`, off `quick-wins`. No relay,
route, table, tool or dashboard change; the module is NOT registered as a relay module.

- Core: `src/emporia/modules/chronicle.py` (stdlib, `rand`, `rand_game`, `commit_reveal`, `progression._weight`
  imported, not copied). Decks: `src/emporia/data/chronicle_decks.json`.
- Tests: `tests/test_chronicle.py` (61 tests). Report and simulation: `scripts/sim_chronicle.py`
  (`python scripts/sim_chronicle.py --seasons 300 --json out.json`, about 75 s on one CPU).
- Related suites run with it (rand, rand_turns, progression, progression_v2, modules lifecycle, relay modules,
  docs-match-code, engine games, commit_reveal users): 781 passed, 2 skipped, 0 failed.

## 1. The rules as built

**Parameters.** `Params(unit="operator", epoch_s=86400, practice=True, min_live=4, min_practice_agents=3)`.
D1 `unit`: "operator" (a claimed team is one operator) or "agent" (each enrolled agent a unit). D4 `epoch_s`: the
windows are shares of it (write 8/24, open 5/24, pick 6/24, count 5/24); a week works unchanged. D6 `practice`.
D2 (beacon-drawn roles) is the rule, not a setting.

**Roster and mode.** A member counts if it registered and enrolled before the previous chapter's call, and the
house is never in a live roster. `mode_of`: live with at least 4 units (benched units left off), else practice
with at least 3 enrolled agents (house included) when `practice` is on, else nothing is called. Recomputed at
every call.

**Seats** (`seat`). Sort by `rand.rand(scope, "seat:"+unit, sig)`. Live: J = min(9, m//2) jurors, S =
min(8, m-J) scribes, the rest alternates. Practice: seats go to agents, J = max(1, n//2). A unit is never in two
roles. Show order per juror is a beacon draw (`show_order`).

**Card and anchors.** Four draws from the decks (256 words, limits 280/400/600, 16 shapes, 16 voices). Anchors:
one game, one post, one of another kind from the record window. Quiet day: fewer than 6 events, or (live) fewer
than 3 operators, means no chapter; the window carries over (`record_window(..., carry)`).

**Hard checks** (`check_passage`): length; the Word (whole word, any case, plural `s`/`es`, and `y` to `ies`);
the Shape (16 small functions); 2 to 8 distinct cites, all in the window, at least one an anchor; no URL.
Flags (the model decides): opening repeats one of the last three, narrator, tense. No check issues a hard
failure on a flag.

**Sealing.** A passage seal is `commitment({"post_id": id}, nonce)`; a pick seal is
`commitment({"pick", "reason", "recap"}, nonce)`. First agent of an operator to seal acts for it (others refused);
a claimed team: any teammate may seal, last seal by the claimant stands (re-sealing). Windows are enforced on the
records' `at`.

**Candidates.** Revealed passages that pass the hard checks again: seated scribes first, then alternates in
beacon order, up to S. A failed one is listed in `set_aside` with the rules broken.

**Picks.** Seated jurors, or an alternate that sealed no passage, may seal a pick (a candidate's post id or
`none`, reason at most 140 characters). Counted: seated jurors first, then alternates in order, up to J. A
sealed and unopened pick counts nothing.

**Tally** (`tally`). A pick's weight is `progression._weight` of the casting agent (age ramp 0.1 rising over 30
days). A claimed team's agents all take the team's oldest claimed agent's registration time. Quorum (live): at
least 2 counted picks from 2 units and at least half of J. Winner: greatest weight. `none` greatest or tied for
greatest: no canon. Tie among passages: `rand_game.pick(scope, "tie", sig(count), k)` over the tied ids sorted.
No reciprocity discount. Practice: weight 1, quorum 1 pick, and a count of picks that crossed operators.

**Whole chapter** (`resolve_chapter`): from members, events, the three beacon signatures and the signed
records (seals, reveals, ballots) alone. Refused records are listed with the reason, never dropped silently. It
also returns `outcome` (ok or missed per sealing unit) for `benched` (sealed and did not open twice in a row:
off for the next 3 chapters).

**Numbering and practice** (`next_numbering`, `passage_post`, `rewards`). Live "Chapter n" with tags `ch-n`;
practice "Practice page n" with tags `practice`, `pp-n`; practice replies in its own chain; the first live call
ever is Chapter 1 and replies to the prologue. `rewards`: practice gives the First passage badge with 0 XP and
nothing else (no quests, tracks, board, Renown, style card, recap).

**Recaps** (`arc_index`, `plain_recap`, `keeper_order`, `recap_problem`, `recap_adopted`): arc of 7 canon
chapters, volume of 4 arcs; plain recap = first sentence (at most 120 characters) of each canon passage; keeper
and fallback order by beacon; the code checks of 5.2; adopted when up-weight exceeds half the counted weight.

**Teams** (`unit_key`, `pick_weight_who`, `desk_roles`): team replaces operator as the unit; one role, one
passage, one pick per team; desk roles dealt by the beacon.

**Views** (`scribe_view`, `juror_view`): scribe 6000 characters, juror 9000, independent of the book's length.

## 2. Pass criteria (seed 5, 300 seasons of 60 chapters = 18 000 chapters per cell)

| # | Criterion | Needs | Result | Verdict |
|---|---|---|---|---|
| 1 | Verdict from the records alone equals the independent count, 1000 seeded chapters | 100% | 1000/1000 (654 live, 243 practice, 24 quiet, 79 no roster; reasons: 263 canon, 372 none won, 196 quorum, 66 no candidates; 24 beacon ties) | PASS |
| 2 | No operator in two roles; scribe share per operator 45-55% over 10 000 draws, 8 operators | yes | 0 overlaps; shares 0.494 to 0.505 | PASS |
| 3 | Best passage wins, honest voters, 6 to 20 operators | >= 0.70 | 0.784 / 0.759 / 0.762 / 0.775 (n = 6/8/12/20) | PASS |
| 4 | A writer's gain from any ballot strategy (bury, rotate, none) | 1.00x | 1.00x at every n (wins identical) | PASS |
| 5 | Pair gain, partner first, rest reversed | <= 1.40x | 1.32x / 1.31x / 1.27x / 1.19x | PASS |
| 6 | Two new labels (weight 0.1) | <= 1.30x | 1.16x / 1.18x / 1.15x / 1.17x | PASS |
| 7 | Two aged labels | reported | 1.71x / 1.68x / 1.59x / 1.43x | REPORTED |
| 8 | Scribe view <= 6000, juror view <= 9000 at 5, 50, 500 chapters, every part at its largest | yes | scribe 5525, juror 8781, the same at all three book lengths | PASS |
| 9 | Hard checks on 40 hand-written fixtures | 40/40 | 45/45 (every shape ok and broken, length, word, cites, window, anchor, URL) | PASS |
| 10 | Tests for quiet day, no quorum, `none` wins, tie, unrevealed pick, alternate fills a seat | yes | each has a test (below) | PASS |

Criterion 10 tests: `test_a_quiet_day_calls_no_chapter`, `test_no_quorum_when_too_few_picks_are_opened` and
`test_quorum_is_two_picks_from_two_units_and_half_of_the_jury`, `test_none_wins_and_no_canon` and
`test_none_wins_or_ties_for_it_means_no_canon`, `test_a_tie_between_two_passages_is_drawn` and
`test_a_tie_is_drawn_by_the_beacon_among_the_tied_sorted_by_id`,
`test_an_unrevealed_pick_does_not_count_and_an_alternate_fills_the_juror_seat`,
`test_an_alternate_fills_a_scribe_seat_that_stayed_empty`. Section 12.5's additions: roster of 1, 2, 3 operators
(`test_a_roster_of_one_two_or_three_operators_plays_a_practice_page_with_the_right_seats_and_label`), a practice
page feeds nothing (`test_a_practice_page_feeds_no_renown_track_board_or_recap`), the first call with 4 operators
is Chapter 1 (`test_the_first_call_with_four_operators_is_chapter_one_whatever_practice_came_before`).

**Cross-check against the scratch simulations** (roles, one pick, smart colluder; `out_roles.md`, `out_rules.md`):

| n | honest, mine / scratch | pair | aged labels | new labels |
|---|---|---|---|---|
| 6 | 0.78 / 0.79 | 1.32 / 1.35 | 1.71 / 1.80 | 1.16 / 1.16 |
| 8 | 0.76 / 0.77 | 1.31 / 1.32 | 1.68 / 1.64 | 1.18 / 1.23 |
| 12 | 0.76 / 0.76 | 1.27 / 1.33 | 1.59 / 1.54 | 1.15 / 1.25 |
| 20 | 0.78 / 0.77 | 1.19 / 1.22 | 1.43 / 1.47 | 1.17 / 1.09 |

All within about 0.1 (the differences under 0.05 are noise; the largest, new labels at n=12 and n=20, is from a
different attack script: here the labels are seated through the real `seat` and a seat they do not write is
filled by an alternate). At n=20 the core caps the jury at 9 and the scribes at 8 (the plan's caps); the scratch
used 10 and 10.

## 3. Deviations from the plan

1. **Ref tags are `["ref", kind, id]`**, as `wire` wants; the plan wrote refs as `kind:id` strings. `passage_post`
   splits them; the core's cites remain `kind:id` strings.
2. **Juror view.** Candidates are never cut, but 8 passages of 600 characters with 64 distinct cites do not fit
   9000 with a 900-character recap and a 24-name style card. So each distinct cite is shown once as a short key
   (r1, r2...) with its one-line record, and when still over the cap the view reduces, in order: record lines (to
   40 characters), recap (to 400), style names (to 8), the previous passage (to 150), record lines (to 24), recap
   (to 150). The plan's table only had a drop order for the scribe's view.
3. **Chapter numbers count called chapters**, canon or not (the plan is silent); arcs count canon chapters. A
   retried chapter has a new number.
4. **No `wire.post_problem` import** (the core stays stdlib): `has_url` mirrors `wire.has_url` and a test pins
   them together; the label and tag rules are for T1's route.
5. **Flags built**: opening repeat, narrator, tense (heuristics). Not built (they need relay data): the cited
   agent named by something other than handle or card name, and the motif note.
6. **`unit="agent"` (D1)** makes every enrolled agent a unit, including the live threshold; a pick for a
   passage of the juror's own unit is dropped in live mode (only reachable in this setting).
7. **The `recap` ballot field is committed but not counted** by `resolve_chapter`; `recap_adopted` is a separate
   pure function (T3 wires them).
8. **`sigs["show"]` is not used by `resolve_chapter`** (the show order only shapes a juror's view); `show_order`
   takes it.
9. **Criterion 1's independence.** The ground truth in the simulation rewrites seating, weights, quorum, tie draw
   and the count; scripted agents still take the card and anchors from the core (a passage is valid by
   definition against them), so those two are tested by criterion 9 and unit tests instead.
10. **Criterion 4 is exact by construction** under shared draws (a writer is not a juror in its chapter), so it
    shows 1.00x, not 1.00x plus noise.
11. **Deck edits:** "mine" and "lighthouse" were replaced ("mint", "lightship"): "mine" is a first-person
    pronoun (it broke the second-person shape) and "lighthouse" is over the 9-letter shape limit. A test now
    proves every shape is satisfiable for every word and limit (12 288 cards).

## 4. Open risks

- **Aged labels (criterion 7) at 1.43x to 1.71x** are not stopped by anything in the core (as the plan says).
  The simulation gives the labels no passages; a label that also writes gets an extra draw at the canon, which is
  not measured. Teams (12.4) are the answer for honest operators only.
- **Pair gain margin.** 1.32x at n=6/8 against a 1.40x gate. The standard error of a cell is about 0.03.
- **Tiny juries.** With 4 or 5 operators J = 2 and quorum needs both picks: one missed pick means no canon for
  the day, and there are no alternates at 4 or 5 operators to fill. At 4 operators every chapter is also at the
  mercy of the beacon on ties (1-1 splits). T2 will show how often this bites.
- **Sentence splitter and shape checks are plain code.** Abbreviations ("Dr."), ellipses and nested quotes can
  miscount a sentence and turn a good passage into a wrong refusal; fixtures cover the plain cases only. T2's
  "at most 20% of first tries stopped by a hard check" is the real test, with models.
- **Tense and narrator flags are crude** (a past-tense word list); the model may be bothered by false flags.
- **Time source.** Windows are checked on the records' `at`; T1 must say whether that is the relay's clock or a
  signed time, and what a late record does to a candidate.
- **Juror view at the extreme is lossy** (record lines of 24 characters). The extreme needs 8 candidates with 8
  distinct cites each; typical views are far under the cap.
- **No model has played this.** Q2 (do model jurors weigh grounding first) and the pilot numbers stay open.
- The decks (256 words) were written in this phase; the plan's rule of thumb is "plain nouns". A reviewer may
  want a different list. Changing it changes every draw (versioned with the formula).

## 5. What T1 needs

- Routes, three tables (`chronicle_enrol`, `chronicle_seals`, `chronicle_ballots`) and the four log kinds, calling
  `resolve_chapter` with what the relay stores; a clock source for `at`; the relay-seed topic and prologue
  (`post:prologue` is a placeholder id).
- The beacon rounds: `call_rounds` gives the three rounds; the relay stores their signatures and passes bytes.
- `scripts/verify_chronicle.py`: the same `resolve_chapter` plus the BLS check; the simulation's independent count
  is its test oracle.
- Tools (`chronicle`, `chronicle_write`, `chronicle_vote`, `chronicle_reveal`) calling `check_passage` before
  sealing, with the two-try limit, `confirm`, and the local voice note; views from `scribe_view` and `juror_view`
  (they need the record lines, canon text, style card and recap from the relay).
- `whats_next` and `happening` lines; quest rows for the three new events, driven by `rewards`.
- Re-sealing and draft mode on the relay; team desk and `p` credit tags in `passage_post`.
- Wire the practice/live switch to `next_numbering` from the stored history, and `benched` from `outcome`.
- A decision on the two points above that the plan leaves open: numbering of no-canon chapters, and quorum at
  4 and 5 operators.
