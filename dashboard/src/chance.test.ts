import { describe, expect, it } from "vitest";
import type { Session, SessionAction } from "./api";
import { CV_AUCTION, LIARS_DICE, chanceLine, chanceView, isChanceGame } from "./chance";
import { isTablePlay, SET_PLAY_NAMES } from "./setPlay";
import { sessionMatches } from "./sessionSelect";

// The games pack as a viewer sees it: nothing is hidden because nothing is known — the relay
// holds no die and no signal while a game runs; the end publishes what the players revealed.
function session(module_type: string, status: string, participants: string[], data: Record<string, unknown>): Session {
  return {
    session_id: "s-c", module_type, status, participants, current_agent: participants[0], step_number: 1,
    created_at: "2026-11-05T10:00:00Z", state: { data },
  } as Session;
}
const verdict = (result: Record<string, unknown>) => [{
  action_id: "z", agent_id: "a", action_type: "reveal", payload: {}, created_at: "",
  result: { success: true, artifacts: { result } },
}] as SessionAction[];

describe("games pack ids", () => {
  it("counts as set plays in Games, with names", () => {
    for (const m of [LIARS_DICE, CV_AUCTION]) {
      expect(isChanceGame(m)).toBe(true);
      expect(isTablePlay(m)).toBe(true);
      expect(sessionMatches({ module_type: m }, "games", "")).toBe(true);
      expect(sessionMatches({ module_type: m }, "deals", "")).toBe(false);
    }
    expect(isChanceGame("emporia:bargain:v2")).toBe(false);
    expect(SET_PLAY_NAMES[LIARS_DICE]).toBe("liar's dice");
    expect(sessionMatches({ module_type: LIARS_DICE }, "games", CV_AUCTION)).toBe(false);
  });
});

describe("liar's dice", () => {
  const live = {
    players: 3, dice: 2, ones_wild: true, max_bids: 30, move_time_limit_s: 600, turn_started_at: "2026-11-05T10:00:00Z",
    turn_order: ["b", "c", "a"], ended: null,
    bids: [{ n: 1, by: "b", quantity: 2, face: 3 }, { n: 2, by: "c", quantity: 2, face: 4 }],
  };
  it("while it runs: the bids, whose turn from the beacon's order, the clock, no dice", () => {
    const v = chanceView(session(LIARS_DICE, "active", ["a", "b", "c"], live), null, Date.parse("2026-11-05T10:05:00Z"))!;
    expect(v.kind).toBe("dice");
    if (v.kind !== "dice") return;
    expect(v.bids).toHaveLength(2);
    expect(v.toMove).toBe("a");
    expect(v.onesWild).toBe(true);
    expect(v.secondsLeft).toBe(300);
    expect(v.dice).toEqual({});
    expect(chanceLine(v, { status: "active", participants: ["a", "b", "c"] })).toBe("2 bids");
  });

  it("after a call: who called, who revealed, and no turn", () => {
    const v = chanceView(session(LIARS_DICE, "active", ["a", "b", "c"], {
      ...live, ended: { how: "liar", by: "a", bid: 2, bidder: "c" }, rand: { reveals: { a: {}, b: {} } },
    }))!;
    if (v.kind !== "dice") throw new Error("kind");
    expect(v.call).toEqual({ by: "a", bid: 2, bidder: "c" });
    expect(v.revealed).toEqual(["a", "b"]);
    expect(v.toMove).toBeNull();
    expect(v.secondsLeft).toBeNull();
  });

  it("when over: every die the relay published, the result and the scores", () => {
    const v = chanceView(session(LIARS_DICE, "completed", ["a", "b", "c"], {
      ...live, ended: { how: "liar", by: "a", bid: 2, bidder: "c" },
      deal: { hands: { a: { values: [1, 2] }, b: { values: [3, 3] }, c: { values: [6, 4] } } },
    }), verdict({ winner: "a", termination: "bid_failed", points: { a: 1, b: 0, c: -1 }, count: 1 }))!;
    if (v.kind !== "dice") throw new Error("kind");
    expect(v.dice.b).toEqual([3, 3]);
    expect(v.result?.points).toEqual({ a: 1, b: 0, c: -1 });
    expect(chanceLine(v, { status: "completed", participants: ["a", "b", "c"] })).toBe("a won");
  });
});

describe("common-value auction", () => {
  it("sealed bids are only counted; the signals are not known to the view", () => {
    const v = chanceView(session(CV_AUCTION, "active", ["a", "b", "c"], {
      players: 3, spread: 10, phase: "commit", commitments: { a: "ab", b: "cd" }, revealed: {},
      move_time_limit_s: 600, phase_started_at: "2026-11-05T10:00:00Z", base_value: 88,
    }), null, Date.parse("2026-11-05T10:01:00Z"))!;
    if (v.kind !== "auction") throw new Error("kind");
    expect(v.committed).toEqual(["a", "b"]);
    expect(v.bids).toEqual({});
    expect(v.base).toBe(88);
    expect(v.signals).toEqual({});
    expect(v.secondsLeft).toBe(540);
    expect(chanceLine(v, { status: "active", participants: ["a", "b", "c"] })).toBe("2/3 sealed");
  });

  it("when over: the signals, the value, the curse", () => {
    const v = chanceView(session(CV_AUCTION, "completed", ["a", "b", "c"], {
      players: 3, spread: 10, phase: "secrets", commitments: { a: "1", b: "2", c: "3" }, revealed: { a: 90, b: 120, c: 70 },
      rand: { reveals: { a: {}, b: {}, c: {} } }, deal: { hands: { a: { values: 95 }, b: { values: 103 }, c: { values: 88 } } },
    }), verdict({ winner: null, termination: "winners_curse", value: 100, bidder: "b", winning_bid: 120, points: { a: 0, b: -20, c: 0 } }))!;
    if (v.kind !== "auction") throw new Error("kind");
    expect(v.signals).toEqual({ a: 95, b: 103, c: 88 });
    expect(v.bids.b).toBe(120);
    expect(v.result?.termination).toBe("winners_curse");
    expect(chanceLine(v, { status: "completed", participants: ["a", "b", "c"] })).toBe("winners curse");
    expect(v.secondsLeft).toBeNull();
  });

  it("is null for any other game", () => {
    expect(chanceView(session("emporia:blotto:v2", "active", ["a", "b"], {}))).toBeNull();
  });
});
