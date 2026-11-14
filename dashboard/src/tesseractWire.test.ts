// Tesseract as the relay really serves it: tables played through the relay's test client and dumped to
// src/fixtures/tesseract/*.json (tests/test_tesseract_fixtures.py writes and replays them). Not hand-written shapes.
import { describe, expect, it } from "vitest";
import type { Session, SessionAction } from "./api";
import { buildTimeline, arenaKind, type Frame } from "./arena";
import { isTablePlay, SET_PLAY_NAMES } from "./setPlay";
import { isTesseract, pushText, tesseractResult, tesseractVerdict, type TesseractFrame } from "./tesseract";
import table from "./fixtures/tesseract/table.json";
import mid from "./fixtures/tesseract/mid.json";
import waiting from "./fixtures/tesseract/waiting.json";

type Fx = { session: unknown; actions: unknown };
const session = (fx: Fx) => fx.session as unknown as Session;
const actions = (fx: Fx) => fx.actions as unknown as SessionAction[];
const timeline = (fx: Fx) => buildTimeline(session(fx), actions(fx));
const frames = (fx: Fx) => { const t = timeline(fx); return t.steps.map((_, i) => t.frameAt(i) as TesseractFrame); };

describe("Tesseract in the Arena", () => {
  it("is a table play with a name, and its own kind", () => {
    expect(isTesseract("emporia:tesseract:v1")).toBe(true);
    expect(isTablePlay("emporia:tesseract:v1")).toBe(true);
    expect(SET_PLAY_NAMES["emporia:tesseract:v1"]).toBe("Tesseract");
    expect(arenaKind("emporia:tesseract:v1")).toBe("tesseract");
    for (const fx of [table, mid, waiting]) expect(timeline(fx).kind).toBe("tesseract");
  });

  it("a finished table steps through every action, and the last step carries the relay's result", () => {
    const t = timeline(table), acts = actions(table);
    expect(t.steps.length).toBe(acts.length + 1);
    expect(t.finished).toBe(true);
    const fs = frames(table);
    const last = fs[fs.length - 1];
    const relay = (acts[acts.length - 1].result?.artifacts as any)?.result;
    expect(last.phase).toBe("done");
    expect(last.rounds.map((r) => r.epoch)).toEqual([1, 2, 3, 4, 5]);
    expect(last.result?.winner).toBe(relay.winner);
    expect(last.result?.points).toEqual(relay.points);
    expect(last.result?.order).toEqual(relay.order);
    expect(tesseractVerdict(last.result!)).toContain(`${relay.winner} won`);
    // before the end there is no verdict and no target
    expect(fs.slice(0, -1).every((f) => f.result === null)).toBe(true);
  });

  it("a round shows only once its pushes are opened, and who sat out or was put out", () => {
    const fs = frames(table);
    const counts = fs.map((f) => f.rounds.length);
    expect(counts[0]).toBe(0);
    expect(counts).toEqual([...counts].sort((a, b) => a - b));
    const last = fs[fs.length - 1];
    expect(last.rounds[2].silent.length).toBe(1);     // fx_0 sat round 3 out
    expect(last.rounds[3].late.length).toBe(1);       // fx_3 sealed and did not open round 4
    expect(last.seats.filter((s) => s.state === "out").length).toBe(1);
    // each round's mix is the one the state publishes, and the four shares add to just under 10000
    const pub = (session(table).state as any).data.published;
    for (const r of last.rounds) {
      expect(r.mix).toEqual(pub[String(r.epoch)].mix);
      expect(r.mix.reduce((a, b) => a + b, 0)).toBeGreaterThan(9900);
    }
  });

  it("never draws a commitment, a nonce or a secret, at any step", () => {
    const hidden = new Set<string>();
    for (const a of actions(table)) {
      const p = a.payload as Record<string, unknown>;
      for (const k of ["commitment", "nonce", "s"]) if (typeof p[k] === "string") hidden.add(p[k] as string);
    }
    expect(hidden.size).toBeGreaterThan(10);
    const text = JSON.stringify([frames(table), timeline(table).steps]);
    for (const h of hidden) expect(text.includes(h), h.slice(0, 12)).toBe(false);
  });

  it("labels the steps in words, never with what was sealed", () => {
    const labels = timeline(table).steps.map((s) => s.label);
    expect(labels[0]).toBe("the table as the beacon dealt it");
    expect(labels.some((l) => /^sealed its push for round 1$/.test(l))).toBe(true);
    expect(labels.some((l) => /^opened its push of round 1: .+/.test(l))).toBe(true);
    expect(labels).toContain("revealed its secret");
    expect(labels.filter((l) => /^sealed/.test(l)).every((l) => !/[+−]\d/.test(l))).toBe(true);
  });

  it("a running table: the third round half sealed, nothing of it drawn", () => {
    const fs = frames(mid);
    const last = fs[fs.length - 1];
    expect([last.epoch, last.phase, last.result]).toEqual([3, "seal", null]);
    expect(last.rounds.map((r) => r.epoch)).toEqual([1, 2]);
    expect(last.seats.map((s) => s.state)).toEqual(["sealed", "sealed", "waiting", "waiting"]);
    expect(JSON.stringify(last)).not.toContain("XZ>Z");
  });

  it("a waiting table has one step and nobody moved", () => {
    const t = timeline(waiting), f = t.frameAt(0) as TesseractFrame;
    expect(t.steps.length).toBe(1);
    expect(f.seats.length).toBe(2);
    expect(f.seats.every((s) => s.state === "waiting")).toBe(true);
    expect(f.rounds).toEqual([]);
  });

  it("reads pushes and the result as the helpers say", () => {
    expect(pushText({ "WX>W": 4, "YZ>Z": -3 })).toBe("WX>W +4, YZ>Z −3");
    expect(pushText({})).toBe("nothing");
    expect(tesseractResult([])).toBeNull();
    const f: Frame = timeline(table).frameAt(0);
    expect(f.kind).toBe("tesseract");
  });
});
