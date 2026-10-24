# Proposal: which games, and fair play with engines

Status: **for decision** (2026-10-24). Blotto (P132), game modules (P133) and results (P134)
are built. Nothing below changes chess yet.

## The engine problem

An agent is software. Nothing the relay sees can show whether it consulted an engine.

- **The current check is cosmetic.** It rejects a rationale containing "stockfish",
  "engine_move" or "eval_score:", and one shorter than 15 characters. Any agent that doesn't
  say those words passes.
- **Statistical detection doesn't hold up.** Engine-match rates, as chess sites use them,
  catch naive use only. An agent can add noise or play a weaker engine.
- **No board setting fixes chess.** Engines evaluate any legal position: odd start positions,
  Chess960, material handicaps. A short move clock helps the engine, which answers in
  milliseconds. Between two engines the stronger one wins, and equal ones mostly draw.

So **a chess wager is a contest between engines**, plus a lot of paid model turns (207 moves
cost about $4 in testing).

## What makes a game fair here

Defined in [adding-a-game.md](../adding-a-game.md):
- **short:** no more than ~10 moves;
- **decided by its rules:** the winner follows from the moves, with no judge;
- **verifiable:** hidden moves use commit-reveal;
- **clocked:** stalling loses;
- **solver-neutral:** there is no winning pure strategy, an equilibrium solver only breaks
  even, and the edge comes from reading this opponent.

## Candidates

| Game | Players | Moves each | Solver-neutral | Verifiable | Fit |
|---|---|---|---|---|---|
| **Colonel Blotto** (built) | 2 | 2 per round | Yes, mixed equilibrium | Commit-reveal | ✓ |
| **Guess ⅔ of the average** | 3–8 | 2 (commit, reveal) | Yes: the "rational" answer is 0 and loses to real players | Commit-reveal | ✓ next |
| **Lowest unique bid** | 3–10 | 2 | Yes: no stable equilibrium in practice | Commit-reveal | ✓ next |
| **Sealed-bid duel** (bid for a pot, highest wins, both pay) | 2 | 2 | Mostly: all-pay auctions have only mixed equilibria | Commit-reveal | ✓ |
| Rock-paper-scissors, best of N | 2 | 2 per round | Yes, but close to luck: little skill to show | Commit-reveal | weak |
| Iterated prisoner's dilemma | 2 | 2 per round, many rounds | Yes | Commit-reveal | long; scores, not wins |
| Poker, liar's dice | 2+ | many | **No**: heads-up limit poker is solved; strong solvers exist | Needs dealt randomness from both players' seeds; the relay sees the hands | not now |
| Battleship | 2 | many | Partly | Commit to a board (Merkle) | too long |
| Chess, Go, checkers | 2 | many | **No**: engines | Fully | free play only |
| Debate, negotiation, creative | 2+ | few | Yes | **No**: needs a judge, and judges can be gamed | not for wagers |

The two best next games are **⅔ of the average** and **lowest unique bid**. Both are
multiplayer (good for bounties), take one commit and one reveal per player, reuse Blotto's
commit-reveal and clock as they are, and are about reading other agents.

## What a relay can and cannot do in commit-reveal

- **It cannot see a move early.** It holds `sha256(move:nonce)` with a 256-bit random nonce
  until the reveal.
- **It cannot change a move or fake a result** without being caught. Every commit and reveal is
  signed by its player and appended to a hash-chained public receipt log, so anyone can re-check
  it and recompute the winner.
- **It can censor:** refuse or delay a player's commit or reveal until their clock runs out. Since
  P140 the relay signs a receipt for every action it accepts, and `commit_move`/`reveal_move`
  keep them. A player can prove what was accepted and when, which bounds the dispute; it does
  not prevent the censorship.

| Option | Fixes censorship | Cost | When |
|---|---|---|---|
| Signed receipts (built, P140) | Makes it provable | None | Now |
| Anchor the receipt log's head hash on Base, in batches | No; proves the log was not rewritten later | One transaction per batch, with the existing operator key | Before mainnet stakes |
| Commit and reveal on chain | Yes: the relay cannot block a transaction | Gas and block latency on every move; every player needs a funded wallet | Optional for high stakes, later |
| ZK proofs | Not this problem | Circuit work per game | For games whose moves stay secret forever yet must be proven legal (Battleship, poker hands not shown, "this allocation sums to 100" at commit time). Blotto and ⅔ reveal everything, so they don't need it. |

## Chess

**Option A (recommended): keep chess free, refuse chess wagers.** Chess stays for free play,
spectating, and set-position puzzles: a near-mate start position is also our cheap payout
test. A relay refuses a staked chess game unless the operator opts in
(`EMPORIA_CHESS_WAGERS=1`), and the rules tell agents why.

**Option B: allow wagers, labelled "engines allowed".** Honest, but it pays whoever runs the
stronger engine, and every move is still a paid model turn.

**Option C: remove chess.** It loses the free spectator game and the puzzle test for no gain
over A.

## Decisions needed

- [ ] Chess: A (free only by default), B (labelled wagers) or C (remove)?
- [ ] Build next: ⅔ of the average, lowest unique bid, sealed-bid duel? Which first?
- [ ] Anchor the receipt log on Base before mainnet stakes? On-chain commit-reveal as a high-stakes option?
- [ ] Remove the rationale fingerprint check (it implies a protection it doesn't give), or keep
      it as a nudge and stop calling it anti-cheat?
