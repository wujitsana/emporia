import { describe, expect, it } from "vitest";
import type { Session, SessionAction } from "./api";
import {
  BLOTTO, LOWEST_UNIQUE, TWO_THIRDS, clockText, isSetPlay, lowestUniqueVerdict, phaseLine, setPlayView,
  stateData, twoThirdsVerdict,
} from "./setPlay";

const H = "ab".repeat(32);

function session(module_type: string, status: string, participants: string[], data: Record<string, unknown>): Session {
  return {
    session_id: "s-1", module_type, status, participants, current_agent: participants[0], step_number: 1,
    created_at: "2026-11-05T10:00:00Z", state: { data, current_agent: participants[0], step_number: 1, metadata: {} },
  } as Session;
}

describe("set plays", () => {
  it("knows which modules are set plays — chess is not one", () => {
    expect(isSetPlay(BLOTTO)).toBe(true);
    expect(isSetPlay(TWO_THIRDS)).toBe(true);
    expect(isSetPlay(LOWEST_UNIQUE)).toBe(true);
    expect(isSetPlay("emporia:chess:v1")).toBe(false);
    expect(isSetPlay("emporia:service:v1")).toBe(false);
  });

  it("reads the relay's state.data, and a flat state too", () => {
    expect(stateData({ state: { data: { phase: "reveal" } } } as unknown as Session)).toEqual({ phase: "reveal" });
    expect(stateData({ state: { phase: "commit" } } as unknown as Session)).toEqual({ phase: "commit" });
  });
});

describe("blotto", () => {
  it("live, mid-reveal: one move shown, the other still hidden, nobody lit", () => {
    const v = setPlayView(session(BLOTTO, "active", ["alpha", "beta"], {
      battlefields: 5, troops: 100, rounds: 1, round: 1, phase: "reveal",
      commitments: { alpha: H, beta: H }, revealed: { alpha: [30, 30, 30, 5, 5] }, history: [], wins: { alpha: 0, beta: 0 },
    }));
    expect(v.phase).toBe("reveal");
    expect(v.seats.map((s) => s.state)).toEqual(["revealed", "committed"]);
    expect(v.seats[0].move).toEqual([30, 30, 30, 5, 5]);
    expect(v.seats[1].move).toBeUndefined();
    expect(v.seats[1].hash).toBe(H.slice(0, 8));
    expect(v.pending).toEqual(["beta"]);
    expect(v.verdict).toBeNull();
    expect(phaseLine(v)).toBe("reveal · 1 of 2");
  });

  it("finished: the winner and the fields that won it", () => {
    const v = setPlayView(session(BLOTTO, "completed", ["alpha", "beta"], {
      battlefields: 5, troops: 100, rounds: 1, round: 2, phase: "commit", commitments: {}, revealed: {},
      history: [{ round: 1, allocations: { alpha: [30, 30, 30, 5, 5], beta: [20, 20, 20, 20, 20] }, winner: "alpha" }],
      wins: { alpha: 1, beta: 0 },
    }));
    expect(v.phase).toBe("done");
    expect(v.verdict).toMatchObject({ winner: "alpha", draw: false });
    expect(v.verdict!.why).toBe("alpha took 3 of 5 fields");
    expect(v.blotto!.rounds_done[0].fieldWinners).toEqual(["alpha", "alpha", "alpha", "beta", "beta"]);
    expect(v.seats.find((s) => s.winner)?.agent).toBe("alpha");
  });

  it("the relay's verdict wins over the one computed here", () => {
    const actions = [{ action_id: "a", agent_id: "beta", action_type: "reveal", payload: {}, created_at: "",
      result: { success: true, artifacts: { result: { winner: "beta", outcome_type: "won" } } } }] as SessionAction[];
    const v = setPlayView(session(BLOTTO, "completed", ["alpha", "beta"], {
      rounds: 1, history: [], wins: { alpha: 0, beta: 0 },
    }), actions);
    expect(v.verdict?.winner).toBe("beta");
  });
});

describe("guess two-thirds", () => {
  it("is decided by the pick closest to ⅔ of the average", () => {
    const t = twoThirdsVerdict({ alpha: 33, gamma: 22, delta: 50, kestrel: 15 })!;
    expect(t.average).toBe(30);
    expect(t.target).toBe(20);
    expect(t.verdict.winner).toBe("gamma");
  });

  it("an exact tie for closest is a draw", () => {
    // average 30, target 20: 18 and 22 are both 2 away.
    const t = twoThirdsVerdict({ a: 18, b: 22, c: 50 })!;
    expect(t.verdict).toMatchObject({ winner: null, draw: true });
  });

  it("while picks are hidden there is no target and no winner", () => {
    const v = setPlayView(session(TWO_THIRDS, "active", ["alpha", "gamma", "delta"], {
      phase: "commit", pending: ["delta"], commitments: { alpha: H, gamma: H }, guesses: {},
    }));
    expect(v.pick).toMatchObject({ min: 0, max: 100, target: null });
    expect(v.seats.map((s) => s.state)).toEqual(["committed", "committed", "to_commit"]);
    expect(v.verdict).toBeNull();
    expect(phaseLine(v)).toBe("commit · 2 of 3");
  });

  it("a player who committed and did not reveal is out of a finished game", () => {
    const v = setPlayView(session(TWO_THIRDS, "completed", ["a", "b", "c"], {
      phase: "reveal", pending: ["c"], commitments: { a: H, b: H, c: H }, guesses: { a: 10, b: 40 },
    }));
    expect(v.seats.map((s) => s.state)).toEqual(["revealed", "revealed", "out"]);
    expect(v.verdict?.winner).toBe("a");   // average 25, target 16.7: 10 is closer than 40
  });
});

describe("lowest unique", () => {
  it("the lowest number picked once wins", () => {
    expect(lowestUniqueVerdict({ alpha: 1, beta: 1, gamma: 3, delta: 2 })).toMatchObject({ winner: "delta" });
  });

  it("no unique number is a draw", () => {
    expect(lowestUniqueVerdict({ a: 2, b: 2, c: 5, d: 5 })).toMatchObject({ winner: null, draw: true });
  });

  it("mid-reveal shows the picks so far and waits on the rest", () => {
    const v = setPlayView(session(LOWEST_UNIQUE, "active", ["alpha", "beta", "gamma", "delta"], {
      max_pick: 10, phase: "reveal", pending: ["beta"], commitments: { alpha: H, beta: H, gamma: H, delta: H },
      guesses: { alpha: 2, gamma: 1, delta: 1 },
    }));
    expect(v.pick).toMatchObject({ min: 1, max: 10 });
    expect(v.pending).toEqual(["beta"]);
    expect(v.verdict).toBeNull();
    expect(v.seats.find((s) => s.agent === "beta")?.state).toBe("committed");
  });
});

describe("clock", () => {
  it("says minutes, then seconds, then that time is up", () => {
    expect(clockText(null)).toBeNull();
    expect(clockText(600)).toBe("10 min left");
    expect(clockText(45)).toBe("45 s left");
    expect(clockText(0)).toBe("time is up");
  });
});
