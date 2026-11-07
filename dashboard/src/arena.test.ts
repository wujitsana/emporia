import { describe, expect, it } from "vitest";
import { buildTimeline, clampStep, type Frame, effectiveActions, modelStanding, playerName } from "./arena";
import { AUCTION, BARGAIN, BLOTTO, DICE, TWO_THIRDS, act } from "./arenaFixtures";
import { hashForView, stepFromHash } from "./dashboardRoute";

const text = (v: unknown) => JSON.stringify(v);

describe("the timeline has a step for every action, plus the start", () => {
  it.each([["blotto", BLOTTO], ["two-thirds", TWO_THIRDS], ["dice", DICE], ["auction", AUCTION], ["bargain", BARGAIN]] as const)(
    "%s", (kind, fx) => {
      const t = buildTimeline(fx.session, fx.actions);
      expect(t.kind).toBe(kind);
      expect(t.steps).toHaveLength(fx.actions.length + 1);
      expect(t.steps[0].type).toBe("start");
    });

  it("leaves a rejected action out", () => {
    const bad = { ...act("alpha", "bid", { quantity: 9, face: 1 }), result: { success: false } };
    expect(effectiveActions([bad, ...DICE.actions])).toHaveLength(DICE.actions.length);
  });
});

describe("a set play shows sealed until the reveal step", () => {
  const t = buildTimeline(BLOTTO.session, BLOTTO.actions);
  it("seats are waiting, then sealed, then revealed one at a time", () => {
    const f = (i: number) => t.frameAt(i) as Extract<Frame, { round: number }>;
    expect(f(0).slots.map((s) => s.state)).toEqual(["waiting", "waiting"]);
    expect(f(1).slots.map((s) => s.state)).toEqual(["sealed", "waiting"]);
    expect(f(2).slots.map((s) => s.state)).toEqual(["sealed", "sealed"]);
    expect(f(3).slots.map((s) => s.state)).toEqual(["revealed", "sealed"]);
    expect(f(3).slots[0].move).toEqual([30, 30, 30, 5, 5]);
    expect(f(4).slots.map((s) => s.state)).toEqual(["revealed", "revealed"]);
  });
  it("never carries a hidden move or a commitment before its reveal", () => {
    for (const i of [1, 2]) {
      const s = text(t.frameAt(i));
      expect(s).not.toContain("30");
      expect(s).not.toContain("abab");
    }
    expect(text(t.frameAt(3))).not.toContain("35");          // beta's allocation, still sealed
  });
  it("shows the verdict only on the last step", () => {
    expect((t.frameAt(3) as { verdict: unknown }).verdict).toBeNull();
    expect((t.frameAt(4) as { verdict: { winner: string } }).verdict?.winner).toBe("alpha");
    expect((t.frameAt(4) as { fields: unknown[] }).fields).toEqual(["alpha", "alpha", "alpha", "beta", "beta"]);
  });
  it("two-thirds reveals picks the same way", () => {
    const tt = buildTimeline(TWO_THIRDS.session, TWO_THIRDS.actions);
    expect(text(tt.frameAt(2))).not.toContain("20");
    expect((tt.frameAt(3) as { slots: { move?: number }[] }).slots[0].move).toBe(20);
    expect((tt.frameAt(4) as { verdict: { winner: string } }).verdict?.winner).toBe("alpha");
  });
  it("a multi-round blotto keeps the earlier rounds", () => {
    const rounds = [...BLOTTO.actions, act("alpha", "commit", { commitment: "cd".repeat(32) })];
    const s = { ...BLOTTO.session, state: { data: { ...(BLOTTO.session.state as { data: object }).data, rounds: 2 } } };
    const f = buildTimeline(s, rounds).frameAt(5) as { round: number; past: unknown[]; slots: { state: string }[] };
    expect(f.round).toBe(2);
    expect(f.past).toHaveLength(1);
    expect(f.slots.map((x) => x.state)).toEqual(["sealed", "waiting"]);
  });
});

describe("liar's dice", () => {
  const t = buildTimeline(DICE.session, DICE.actions);
  it("shows bids as they come and the challenge on its bid", () => {
    const f = (i: number) => t.frameAt(i) as { bids: unknown[]; call: { by: string; bid: number } | null; dice: unknown };
    expect(f(1).bids).toHaveLength(1);
    expect(f(2).bids).toHaveLength(2);
    expect(f(3).call).toMatchObject({ by: "alpha", bid: 2 });
  });
  it("keeps the dice hidden until the last step", () => {
    for (let i = 0; i < t.steps.length - 1; i++) expect((t.frameAt(i) as { dice: unknown }).dice).toBeNull();
    expect((t.frameAt(5) as { dice: Record<string, number[]> }).dice.alpha).toEqual([1, 4, 6]);
  });
});

describe("the auction", () => {
  const t = buildTimeline(AUCTION.session, AUCTION.actions);
  it("seals bids, reveals them one by one, and shows V only at the end", () => {
    const f = (i: number) => t.frameAt(i) as { slots: { state: string; move?: number }[]; value: number | null; signals: unknown };
    expect(f(2).slots.map((s) => s.state)).toEqual(["sealed", "sealed"]);
    expect(f(3).slots[0]).toMatchObject({ state: "revealed", move: 52 });
    expect(f(3).slots[1].state).toBe("sealed");
    expect(f(5).value).toBeNull();
    expect(f(6).value).toBe(55);
    expect(f(6).signals).toEqual({ alpha: 47, beta: 53 });
  });
});

describe("bargain", () => {
  const t = buildTimeline(BARGAIN.session, BARGAIN.actions);
  it("shows the offers so far, the acceptance, and the hands only at the end", () => {
    const f = (i: number) => t.frameAt(i) as { offers: unknown[]; ended: { how: string } | null; hands: unknown };
    expect(f(1).offers).toHaveLength(1);
    expect(f(2).offers).toHaveLength(2);
    expect(f(2).ended).toBeNull();
    expect(f(3).ended?.how).toBe("accepted");
    expect(f(2).hands).toBeNull();
    expect(f(3).hands).not.toBeNull();
  });
});

describe("an unknown game falls back to a log, without secrets", () => {
  it("lists the actions so far and drops commitments and nonces", () => {
    const s = { ...BLOTTO.session, module_type: "emporia:mystery:v1" };
    const t = buildTimeline(s, [act("alpha", "note", { text: "hi", nonce: "SECRETNONCE" })]);
    expect(t.kind).toBe("log");
    expect(text(t.frameAt(1))).not.toContain("SECRETNONCE");
    expect((t.frameAt(0) as { rows: unknown[] }).rows).toHaveLength(0);
  });
});

describe("links and labels", () => {
  it("a step in the address round-trips", () => {
    const h = hashForView("games", "sess_1", 3);
    expect(h).toBe("#/play/games/sess_1?step=3");
    expect(stepFromHash(h)).toBe(3);
    expect(stepFromHash("#/play/games/sess_1")).toBeNull();
    expect(stepFromHash("#/play/games/sess_1?step=-1")).toBeNull();
    expect(stepFromHash("#/play/games/sess_1?step=abc")).toBeNull();
    expect(hashForView("games", "sess_1", null)).toBe("#/play/games/sess_1");
    expect(hashForView("games", null, 3)).toBe("#/play");
  });
  it("clamps a step past the end", () => {
    expect(clampStep(99, 5)).toBe(4);
    expect(clampStep(2, 5)).toBe(2);
    expect(clampStep(null, 5)).toBeNull();
  });
  it("names models as declared, and says missing or mismatched", () => {
    expect(modelStanding({ status: "revealed", model: "m1" }, true)).toBe("m1 · revealed");
    expect(modelStanding({ status: "mismatch", model: null }, true)).toBe("model mismatch");
    expect(modelStanding({ status: "committed", model: null }, true)).toBe("model missing");
    expect(modelStanding({ status: "committed", model: null }, false)).toBe("model declared · sealed");
    expect(modelStanding({ status: "none", model: null }, true)).toBe("model missing");
  });
  it("shows name@host when known", () => {
    expect(playerName("abc", { abc: "alpha@relay.example" })).toBe("alpha@relay.example");
    expect(playerName("alpha@host", undefined)).toBe("alpha@host");
  });
});
