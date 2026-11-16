// The Tesseract board view's data and phase rules, on the real wire fixtures (tables played through the relay's test
// client; tests/test_tesseract_fixtures.py writes them) and on the rules' vectors. Nothing hand-written.
import { describe, expect, it } from "vitest";
import type { Session, SessionAction } from "./api";
import { buildTimeline } from "./arena";
import { CHANNELS, type TesseractFrame } from "./tesseract";
import { ALL, boardStates, corner4, cornerPositions, distance, mix4, ownersPublic, phaseNote, place3, target4, targetsKnown } from "./tesseractView";
import { rotate4, to3 } from "./ui/graph3d/project4";
import vectors from "./tesseract.vectors.json";
import table from "./fixtures/tesseract/table.json";
import mid from "./fixtures/tesseract/mid.json";
import waiting from "./fixtures/tesseract/waiting.json";

type Fx = { session: unknown; actions: unknown };
const frames = (fx: Fx) => {
  const t = buildTimeline(fx.session as Session, fx.actions as SessionAction[]);
  return t.steps.map((_, i) => t.frameAt(i) as TesseractFrame);
};
const last = (fx: Fx) => frames(fx).at(-1)!;
const board = (fx: Fx) => (fx.session as any).state.data.board;

describe("the topology in TypeScript is the relay's", () => {
  it("24 channels in the rules' fixed order", () => {
    expect([...CHANNELS]).toEqual(vectors.channels);
    expect(board(mid).channels).toEqual(vectors.channels);
  });
});

describe("what the view shows at each phase", () => {
  it("a waiting table has no network, so nothing is drawn", () => {
    expect(boardStates(last(waiting))).toEqual([]);
  });

  it("a running table shows the opening and the rounds opened so far, never the sealed round", () => {
    const f = last(mid);
    const states = boardStates(f);
    expect(states.map((s) => s.label)).toEqual(["opening", "round 1", "round 2"]);
    expect(f.phase).toBe("seal");
    // the round being sealed carries no pushes and no secret: the state after round 2 is the last one
    expect(states.at(-1)!.channels.every((c) => c.pushes.length <= 4)).toBe(true);
    expect(JSON.stringify(f)).not.toMatch(/commitment|nonce/);
    expect(phaseNote(f)).toContain("sealed, not shown");
  });

  it("each state is the relay's published network: widths, mix, dark and pushes", () => {
    const f = last(table);
    const states = boardStates(f);
    const pub = (table.session as any).state.data.published;
    expect(states.length).toBe(6);
    expect(states[0].channels.map((c) => c.width)).toEqual(pub.opening.widths);
    expect(states[0].mix).toEqual(pub.opening.mix);
    for (let e = 1; e <= 5; e++) {
      const st = states[e], rec = pub[String(e)];
      expect(st.channels.map((c) => c.width)).toEqual(rec.widths);
      expect(st.mix).toEqual(rec.mix);
      expect(st.channels.filter((c) => c.dark).map((c) => c.id)).toEqual(CHANNELS.filter((c) => rec.dark.includes(c)));
      // net push per channel is the sum of everyone's pushes on it, and widths move by exactly that (inside 0..12)
      const sum: Record<string, number> = {};
      for (const p of Object.values(rec.pushes) as Record<string, number>[]) for (const [c, n] of Object.entries(p)) sum[c] = (sum[c] ?? 0) + n;
      for (const c of st.channels) expect(c.net).toBe(sum[c.id] ?? 0);
      const prev = states[e - 1].channels;
      st.channels.forEach((c, k) => expect(c.width).toBe(Math.max(0, Math.min(12, prev[k].width + c.net))));
    }
  });

  it("one player's pushes filter the net column, and owners are public only once a round is opened", () => {
    const f = last(table);
    expect(ownersPublic(f)).toBe(true);
    const who = f.players[0];
    const all = boardStates(f, ALL)[1], one = boardStates(f, who)[1];
    const mine = (table.session as any).state.data.published["1"].pushes[who] as Record<string, number>;
    for (const c of one.channels) expect(c.net).toBe(mine[c.id] ?? 0);
    expect(one.channels.map((c) => c.pushes)).toEqual(all.channels.map((c) => c.pushes));
    expect(ownersPublic(frames(table)[0])).toBe(false);
  });

  it("the pull table shows only for the network as it stands now", () => {
    const fm = last(mid), states = boardStates(fm);
    expect(states.slice(0, -1).every((s) => s.channels.every((c) => c.pull === null))).toBe(true);
    const now = states.at(-1)!.channels;
    expect(now.every((c) => c.pull && c.pull.up.length === 4 && c.pull.down.length === 4)).toBe(true);
    expect(now[12].pull).toEqual(board(mid).pull[CHANNELS[12]]);
    // a replay step before the end: the relay's pull belongs to the end, so it is not shown
    const early = frames(table).find((f) => f.rounds.length === 2)!;
    expect(boardStates(early).every((s) => s.channels.every((c) => c.pull === null))).toBe(true);
  });

  it("targets appear only at the end, and the distance is the score's four-gap sum", () => {
    const fs = frames(table);
    expect(fs.slice(0, -1).every((f) => Object.keys(targetsKnown(f)).length === 0)).toBe(true);
    const end = fs.at(-1)!, t = targetsKnown(end);
    expect(Object.keys(t).sort()).toEqual(Object.keys((table.actions as any).at(-1).result.artifacts.result.targets).sort());
    expect(distance([1000, 3000, 3000, 3000], [10, 30, 30, 30])).toBe(0);
    expect(distance([2000, 2000, 3000, 3000], [10, 30, 30, 30])).toBe(2000);
    expect(distance(vectors.opening.mix, vectors.targets_dealt["agent-a"])).toBe(
      vectors.opening.mix.reduce((s, m, k) => s + Math.abs(m - 100 * vectors.targets_dealt["agent-a"][k]), 0));
  });

  it("a practice table is marked practice", () => {
    const fx = { session: { ...(mid.session as any), config: { ...(mid.session as any).config, practice: true } }, actions: mid.actions };
    expect(last(fx).practice).toBe(true);
    expect(last(mid).practice).toBe(false);
  });
});

describe("the geometry", () => {
  it("outputs are the four axes and the mix stays inside their tetrahedron", () => {
    expect(corner4("W")).toEqual([0.5, -0.5, -0.5, -0.5]);
    expect(corner4("WXYZ")).toEqual([0.5, 0.5, 0.5, 0.5]);
    expect(corner4("")).toEqual([-0.5, -0.5, -0.5, -0.5]);
    const m = [2488, 2977, 2773, 1728];
    const p = mix4([...m]);
    // barycentric: the shares are the weights of the four output corners
    const out = ["W", "X", "Y", "Z"].map(corner4);
    const tot = m.reduce((a, b) => a + b, 0), w = m.map((x) => x / tot);
    for (let k = 0; k < 4; k++) expect(p[k]).toBeCloseTo(out.reduce((s, c, i) => s + w[i] * c[k], 0), 9);
    expect(target4([40, 30, 20, 10])[0]).toBeCloseTo(-0.1, 9);
  });

  it("the double rotation keeps lengths in 4D, a zero turn is the identity, and straight lines stay straight in 3D", () => {
    const p = [0.3, -0.2, 0.5, 0.1] as const;
    const q = rotate4(p, 0.7, 1.1);
    expect(Math.hypot(...q)).toBeCloseTo(Math.hypot(...p), 9);
    expect(rotate4(p, 0, 0)).toEqual([...p]);
    const A = [0.5, -0.5, 0.2, 0.3] as const, B = [-0.4, 0.3, -0.1, -0.2] as const;
    const mid4 = A.map((x, i) => (x + B[i]) / 2) as unknown as typeof A;
    const pa = to3(rotate4(A, 0.4, 0.9)), pb = to3(rotate4(B, 0.4, 0.9)), pm = to3(rotate4(mid4, 0.4, 0.9));
    // the projected midpoint lies on the segment between the projected ends (collinear)
    const u = pb.map((x, i) => x - pa[i]), v = pm.map((x, i) => x - pa[i]);
    const cross = [u[1] * v[2] - u[2] * v[1], u[2] * v[0] - u[0] * v[2], u[0] * v[1] - u[1] * v[0]];
    expect(Math.hypot(...cross)).toBeLessThan(0.02 * Math.hypot(...u) * Math.hypot(...v) + 1e-6);
  });

  it("all 14 role corners get a position that fits the kit's unit sphere, at any angle", () => {
    for (const a of [0, 1, 2.5, 5]) {
      const pos = cornerPositions(a, a * 0.6);
      expect(pos.size).toBe(14);
      for (const v of pos.values()) expect(Math.hypot(...v)).toBeLessThan(1.5);
      expect(Math.hypot(...place3(mix4([2500, 2500, 2500, 2500]), a, a))).toBeLessThan(1.5);
    }
  });
});
