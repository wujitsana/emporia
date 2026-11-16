import { describe, expect, it } from "vitest";
import { buildTimeline } from "./arena";
import { SPILLWAY, SPILLWAY_LEVEL, SPILLWAY_PRE, SPILLWAY_THREES } from "./arenaFixtures";
import { boardAt, findFour, isSpillway, liveThrees, spillwayFrameAt, spillwayVerdict, spillwayView } from "./spillway";
import { isTablePlay, SET_PLAY_NAMES } from "./setPlay";

const view = (fx: typeof SPILLWAY) => spillwayView(fx.session, fx.actions)!;
const rows = (b: string[][]) => b.map((r) => r.join(""));
const name = (s: string) => s;

describe("spillway reading", () => {
  it("knows the module and lists it as a table play, named", () => {
    expect(isSpillway("emporia:spillway:v1")).toBe(true);
    expect(isSpillway("emporia:chess:v1")).toBe(false);
    expect(isTablePlay("emporia:spillway:v1")).toBe(true);
    expect(SET_PLAY_NAMES["emporia:spillway:v1"]).toBe("Spillway");
  });

  it("returns null for another game", () => {
    expect(spillwayView({ ...SPILLWAY.session, module_type: "emporia:chess:v1" })).toBeNull();
  });

  it("rebuilds the dealt opening (rows top first)", () => {
    expect(rows(boardAt(view(SPILLWAY), 0))).toEqual(["......", "......", "......", ".BA...", "#ABAB#"]);
  });

  it("drops settle by the row the record names, and the board grows step by step", () => {
    const v = view(SPILLWAY);
    expect(rows(boardAt(v, 1))[3]).toBe(".BAA..");
    expect(rows(boardAt(v, 5))).toEqual(["......", "...A..", "...A..", "BBAAB.", "#ABAB#"]);
  });

  it("without a row the stone settles on what is below it", () => {
    const v = { ...view(SPILLWAY), moves: [{ n: 1, by: "alpha", channel: 4, row: null, gate: null, round: null }] };
    expect(boardAt(v, 1)[3][3]).toBe("A");
  });

  it("finds four in a line and counts live threes", () => {
    const v = view(SPILLWAY);
    const end = boardAt(v, v.moves.length);
    expect(findFour(end)).toEqual({ mark: "A", line: [[4, 1], [4, 2], [4, 3], [4, 4]] });
    expect(findFour(boardAt(v, 4))).toBeNull();
    const t = view(SPILLWAY_THREES);
    expect(liveThrees(boardAt(t, t.moves.length))).toEqual([2, 1]);
    const l = view(SPILLWAY_LEVEL);
    expect(liveThrees(boardAt(l, l.moves.length))).toEqual([1, 1]);
  });
});

describe("spillway frames", () => {
  it("each step shows the gate that was shut for its drop; step 0 has none", () => {
    const v = view(SPILLWAY);
    expect(spillwayFrameAt(v, 0, false).gate).toBeNull();
    expect(spillwayFrameAt(v, 4, false).gate).toBe(4);
    expect(spillwayFrameAt(v, 4, false).last?.channel).toBe(1);
  });

  it("the verdict, line and counts appear only on the last step of a finished game", () => {
    const v = view(SPILLWAY);
    expect(spillwayFrameAt(v, 4, false).line).toBeNull();
    const end = spillwayFrameAt(v, 5, true);
    expect(end.line).toEqual([[4, 1], [4, 2], [4, 3], [4, 4]]);
    expect(end.result?.termination).toBe("four");
    expect(spillwayVerdict(end, end.result!, name)).toBe("alpha wins with four in a line");
  });

  it("at the cap the live threes are drawn, from the result when it has them", () => {
    const f = spillwayFrameAt(view(SPILLWAY_THREES), 8, true);
    expect(f.threes).toEqual([2, 1]);
    expect(f.left).toEqual([0, 0]);
    expect(spillwayVerdict(f, f.result!, name)).toBe("alpha wins on live threes (alpha 2, beta 1)");
  });

  it("a level count names the second mover as the winner", () => {
    const f = spillwayFrameAt(view(SPILLWAY_LEVEL), 8, true);
    expect(f.threes).toEqual([1, 1]);
    expect(f.first).toBe("alpha");
    expect(spillwayVerdict(f, f.result!, name)).toBe("Level on live threes (1 each): beta moved second and wins");
  });

  it("before the deal: no board, and the round that deals it", () => {
    const f = spillwayFrameAt(view(SPILLWAY_PRE), 0, false);
    expect(f.dealt).toBe(false);
    expect(f.nameRound).toBe(4242);
  });

  it("a live board names the next turn from the record's derived facts", () => {
    const s = { ...SPILLWAY.session, status: "active", state: { data: { ...(SPILLWAY.session.state!.data as object), derived: { to_move: "beta", gate: 5, gate_round: 1005, open: [1, 2, 3, 4, 6] } } } };
    const v = spillwayView(s, [])!;
    expect(spillwayFrameAt(v, v.moves.length, false).next).toEqual({ toMove: "beta", gate: 5, round: 1005 });
  });
});

describe("spillway in the Arena", () => {
  it("step labels say channel, row and the gate", () => {
    const t = buildTimeline(SPILLWAY.session, SPILLWAY.actions);
    expect(t.kind).toBe("spillway");
    expect(t.steps[0].label).toBe("the table as the beacon dealt it");
    expect(t.steps[1].label).toBe("dropped in channel 4, row 2; channel 6 was gated");
    expect(t.steps).toHaveLength(6);
  });

  it("a frame per step: the board grows, the verdict waits for the end", () => {
    const t = buildTimeline(SPILLWAY.session, SPILLWAY.actions);
    const f0 = t.frameAt(0);
    const f3 = t.frameAt(3);
    const fe = t.frameAt(5);
    if (f0.kind !== "spillway" || f3.kind !== "spillway" || fe.kind !== "spillway") throw new Error("kind");
    expect(f0.step).toBe(0);
    expect(f3.step).toBe(3);
    expect(f3.result).toBeNull();
    expect(fe.result?.winner).toBe("alpha");
    expect(fe.line).toHaveLength(4);
  });

  it("a rejected move is not a step", () => {
    const acts = [...SPILLWAY.actions];
    acts.splice(1, 0, { ...acts[0], action_id: "rej", result: { success: false } });
    expect(buildTimeline(SPILLWAY.session, acts).steps).toHaveLength(6);
  });
});
