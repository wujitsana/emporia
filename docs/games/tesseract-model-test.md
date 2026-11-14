# Tesseract: real-model test (R4), NOT A VALID RESULT

Status: the run was contaminated by tool use and is reported only as a preliminary, not as an answer to "does a model beat `lite`". A clean rerun needs the tools really disabled and a new call budget.

## Setup
- Rules: recommended (`width_cap=12, open_hi=12, unit_cap=4`), 5 epochs, 4 seats.
- Table: model seat plus bots `random`, `greedy`, `smart` (the report's 4-seat main-tournament mix with its `lite` seat given to the model). Seat shuffled per seed.
- Baseline: the same 20 seeds (`mt-0` .. `mt-19`) with `lite` in that seat. Targets and chances depend on seed and seat only, so the two runs are paired by seed (the bots react to the other seat's moves, so not move-for-move identical).
- Model: `deepseek/deepseek-v4-flash-0731` on provider `nous`, one-shot `hermes -p tesseract-probe chat -Q --oneshot -t "" --ignore-rules --query-file ...`. Prompt about 3.7k characters: rules in short words, target, widths, last mix, last epoch's pushes by seat, the 24-row pull table with "gain toward your target". No strategy hints.
- Harness: `scripts/model_tesseract.py`. Calls logged with prompt size, response, rc.

## What went wrong
1. `-t ""` did not turn tools off. The profile still had web, terminal, file, code_execution and more enabled. In 15 of 89 answered calls the model wrote scratch scripts (and a file-mutation footer was appended) to optimise its push. Those answers are not "plain reading of the prompt". No clean no-tools path was verified; the call cap was used up before it could be tested.
2. Latency: calls often took minutes. 19 calls hit my timeout (empty answer), 5 failed with a gateway error ("Gateway starting: deadline"). The first run died on a timeout exception; the second resumed from the logged answers (41 reused).
3. Cap: 120 calls hit; the last 4 epochs (games mt-17, mt-19) got "no push" because of the cap, 7 more epochs fell back after two failed calls.

## Numbers (preliminary, contaminated, 20 games = small sample)
Score = total in basis points of movement toward the own target; mean with 95% interval.

| set | games | model | lite | model minus lite (paired) | model win rate | lite win rate |
|---|---|---|---|---|---|---|
| all 20 seeds | 20 | 5214 (+-1006) | 5312 (+-984) | -98 (+-751) | 0.40 | 0.45 |
| 13 seeds with no fallback or cap | 13 | 5784 (+-1166) | 5340 (+-1453) | +444 (+-696) | 0.54 | 0.62 |

Both differences are inside their intervals: no difference can be claimed either way. Per seed the model was better in 10 and worse in 10 games (all 20).
Bots in the same games: greedy 5320 / smart 5341 with the model vs 5341 / 5302 with lite; random 23 / 17.

- Invalid output: 0 answers had a malformed or rule-breaking push. All 24 "invalid" counts were empty or error answers (19 timeouts, 5 gateway errors), i.e. infrastructure, not the model. 11 epochs ended as "no push" (7 after two failed calls, 4 by the cap).
- Pushes made: 84 of 89 used 2 channels, 8 units (the same shape as `lite`: two channels, 4 each); 4 used 3 channels, 1 used 4.
- Reasoning seen: short (about 760 characters on average). Typical: states the target and mix, picks the two best "gain" rows from the table, checks the limits ("Sum 8 units on two channels, within limits"). In the 15 tool-using answers it wrote a small search script over the table instead.

## Call count
120 call attempts (prompt files `q/1..120`): 89 answered, 19 timed out, 5 gateway errors, plus calls cut off by the crash. 41 answers were reused from the first run on identical prompts. Hard cap 120 reached.

## Reading
With two-channel, 4-and-4 pushes picked from the "gain" column, the model behaves like `lite` (which is what the table invites), and its score is indistinguishable from `lite` at this sample. Whether it can do better when it must reason without the gain column, or without tools, is not tested.

## To do for a clean run
Disable all toolsets in the throwaway profile (`hermes -p tesseract-probe tools disable ...` for each), make one checked call that confirms no tool call occurs, use a longer or no timeout, then rerun 20 games with a budget of about 100 calls.
