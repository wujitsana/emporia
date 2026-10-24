import { describe, expect, it } from "vitest";
import { verdict } from "./resultsFormat";
import { buildChessReplay, chessSides } from "./chessReplay";
import type { Session } from "./api";

describe("game results (P134)", () => {
  it("says how a game ended", () => {
    expect(verdict({ winner: null, outcome_type: "draw", termination: "rounds" })).toBe("draw — stakes refunded");
    expect(verdict({ winner: "alpha", outcome_type: "won", termination: "timeout" })).toBe("alpha won on time");
    expect(verdict({ winner: "beta", outcome_type: "won", termination: "Termination.CHECKMATE" })).toBe("beta won by checkmate");
  });
});

describe("chess settings on the board (P131)", () => {
  it("reads stored colours — the creator may play black", () => {
    const s = { participants: ["alpha", "beta"], state: { white: "beta", black: "alpha" } } as unknown as Session;
    expect(chessSides(s)).toEqual({ white: "beta", black: "alpha" });
    const old = { participants: ["alpha", "beta"], state: {} } as unknown as Session;
    expect(chessSides(old)).toEqual({ white: "alpha", black: "beta" });
  });
  it("replays from the game's start position", () => {
    const start = "6k1/5ppp/8/8/8/8/8/R5K1 w - - 0 1";
    const built = buildChessReplay(
      [{ action_type: "move", agent_id: "beta", payload: { uci: "a1a8" } } as never], null, start);
    expect(built.fens[0]).toBe(start);
    expect(built.sans).toEqual(["Ra8#"]);
  });
});
