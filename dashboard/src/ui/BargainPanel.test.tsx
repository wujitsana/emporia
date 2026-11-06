import { describe, expect, it } from "vitest";
import type { Session, SessionAction } from "../api";
import { bargainView } from "../bargain";

// Bargain (docs/proposals/gamify-bargain.md §9): the pool, the offers, whose turn; after the
// end both hands and each claim beside what was held. The dashboard only shows it.
const pool = { silk: 3, spice: 2, amber: 1 };
function session(status: string, data: Record<string, unknown>, extra: Record<string, unknown> = {}): Session {
  return {
    session_id: "s-b", module_type: "emporia:bargain:v1", status, participants: ["alpha", "beta"], current_agent: "beta",
    step_number: 1, created_at: "2026-11-05T10:00:00Z", state: { data }, ...extra,
  } as Session;
}

describe("bargainView", () => {
  it("while it runs: the offers, the clock, the viewer's own values only, no hands", () => {
    const v = bargainView(session("active", {
      pool, total: 10, max_offers: 6, walk_away: 0, move_time_limit_s: 600, turn_started_at: "2026-11-05T10:00:00Z",
      offers: [{ n: 1, by: "alpha", allocation: { alpha: { silk: 2, spice: 0, amber: 1 }, beta: { silk: 1, spice: 2, amber: 0 } },
        claim: { silk: 3, spice: 0, amber: 1 } }],
      ended: null, deal: { commitment: "ab", hands: null },
    }, { private: { values: { silk: 2, spice: 2, amber: 0 } } }), null, Date.parse("2026-11-05T10:05:00Z"));
    expect(v.offers).toHaveLength(1);
    expect(v.offers[0].claim).toEqual({ silk: 3, spice: 0, amber: 1 });
    expect(v.secondsLeft).toBe(300);
    expect(v.hands).toEqual({});
    expect(v.mine).toEqual({ silk: 2, spice: 2, amber: 0 });
  });

  it("after a deal: both hands, the claims as the relay judged them, both scores", () => {
    const actions = [{ action_id: "a", agent_id: "beta", action_type: "accept", payload: {}, created_at: "",
      result: { success: true, artifacts: { result: { winner: "beta", termination: "deal", points: { alpha: 5, beta: 7 },
        claims: { alpha: { claimed: { silk: 3, spice: 0, amber: 1 }, actual: { silk: 2, spice: 1, amber: 2 }, matches: false } } } } } },
    ] as SessionAction[];
    const v = bargainView(session("completed", {
      pool, total: 10, max_offers: 6, walk_away: 0, move_time_limit_s: 600, turn_started_at: "2026-11-05T10:00:00Z",
      offers: [{ n: 1, by: "alpha", allocation: { alpha: { silk: 2, spice: 0, amber: 1 }, beta: { silk: 1, spice: 2, amber: 0 } } }],
      ended: { how: "accepted", by: "beta", offer: 1 },
      deal: { hands: { alpha: { values: { silk: 2, spice: 1, amber: 2 } }, beta: { values: { silk: 1, spice: 3, amber: 1 } } } },
    }), actions);
    expect(v.ended?.how).toBe("accepted");
    expect(v.hands.beta).toEqual({ silk: 1, spice: 3, amber: 1 });
    expect(v.result?.points).toEqual({ alpha: 5, beta: 7 });
    expect(v.result?.claims?.alpha.matches).toBe(false);
    expect(v.secondsLeft).toBeNull();
  });
});
