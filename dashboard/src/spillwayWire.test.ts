// Spillway as the relay really serves it: sessions played through the relay's test client and dumped to
// src/fixtures/spillway/*.json (tests/test_spillway_fixtures.py writes and replays them). Not hand-written shapes.
import { describe, expect, it } from "vitest";
import type { Session, SessionAction } from "./api";
import { boardAt, findFour, liveThrees, spillwayFrameAt, spillwayVerdict, spillwayView } from "./spillway";
import four from "./fixtures/spillway/four.json";
import threes from "./fixtures/spillway/threes.json";
import level from "./fixtures/spillway/level.json";
import coin from "./fixtures/spillway/coin.json";
import mid from "./fixtures/spillway/mid.json";
import pre from "./fixtures/spillway/pre.json";

type Fx = { session: unknown; actions: unknown };
const session = (fx: Fx) => fx.session as unknown as Session;
const actions = (fx: Fx) => fx.actions as unknown as SessionAction[];
const data = (fx: Fx) => (session(fx).state as unknown as { data: Record<string, any> }).data;
const view = (fx: Fx) => spillwayView(session(fx), actions(fx))!;
const rows = (b: string[][]) => b.map((r) => r.join(""));
const name = (s: string) => s;
const finished = { four, threes, level, coin };

describe.each(Object.entries(finished))("a finished %s game from the relay", (word, fx) => {
  it("reads the seats, the opening and every drop with the gate shut for it", () => {
    const v = view(fx), d = data(fx);
    expect(v.seats).toEqual(d.seats);
    expect(v.opening?.first).toBe(d.opening.first);
    expect(v.opening?.silt).toEqual(d.opening.silt);
    expect(v.opening?.stones[0]).toEqual(d.opening.stones[d.seats[0]]);
    expect(v.moves.map((m) => [m.n, m.by, m.channel, m.row, m.gate])).toEqual(d.moves.map((m: any) => [m.n, m.by, m.channel, m.row, m.gate]));
    expect(v.moves.every((m) => m.gate !== null && m.gate !== m.channel)).toBe(true);
    expect(v.toMove).toBeNull();
    expect(v.gateReady).toBe(true);
  });

  it("rebuilds the board the relay stored, and each step the board the relay answered with", () => {
    const v = view(fx), d = data(fx);
    expect(rows(boardAt(v, v.moves.length))).toEqual(d.board);
    actions(fx).forEach((a, k) => {
      const served = (a.result?.new_state as any)?.board;
      expect(rows(boardAt(v, k + 1)), `after drop ${k + 1}`).toEqual(served);
    });
  });

  it("takes the ending from the actions and from the state alone, and they agree", () => {
    const v = view(fx), d = data(fx);
    expect(v.result?.termination).toBe(word);
    expect(d.ended.how).toBe(word);
    expect(v.result?.winner).toBe(d.ended.winner);
    const bare = spillwayView(session(fx))!;      // actions not loaded: `ended` is read
    expect(bare.result?.termination).toBe(word);
    expect(bare.result?.winner).toBe(d.ended.winner);
    expect(bare.result?.line).toEqual(v.result?.line);
  });

  it("agrees with the relay's count of live threes and with its line", () => {
    const v = view(fx);
    const end = boardAt(v, v.moves.length);
    const [a, b] = liveThrees(end);
    const t = v.result!.threes;
    if (word === "four") {
      expect(findFour(end)?.line).toEqual([...v.result!.line!].sort((x, y) => x[0] - y[0] || x[1] - y[1]));
    } else {
      expect(findFour(end)).toBeNull();
      expect([t![v.seats[0]], t![v.seats[1]]]).toEqual([a, b]);
    }
  });
});

describe("the verdict in words", () => {
  const verdict = (fx: Fx) => {
    const v = view(fx);
    const f = spillwayFrameAt(v, v.moves.length, true);
    return { text: spillwayVerdict(f, v.result!, name), f, v };
  };

  it("four in a line", () => {
    const { text, v } = verdict(four);
    expect(text).toBe(`${v.result!.winner} wins with four in a line`);
  });

  it("live threes, with each side's count", () => {
    const { text, f } = verdict(threes);
    expect(f.threes![0]).not.toBe(f.threes![1]);
    expect(text).toContain("wins on live threes");
    expect(text).toContain(`${f.seats[0]} ${f.threes![0]}`);
  });

  it("level goes to the player who moved second", () => {
    const { text, v, f } = verdict(level);
    expect(f.threes![0]).toBe(f.threes![1]);
    expect(v.result!.winner).not.toBe(v.opening!.first);
    expect(text).toContain("Level on live threes");
    expect(text).toContain("moved second and wins");
    expect(text).not.toContain("coin");
  });

  it("a level game decided by a drand coin flip says so, with the counts", () => {
    const { text, v, f } = verdict(coin);
    expect(v.result!.termination).toBe("coin");
    expect(f.threes).not.toBeNull();
    expect(f.threes![0]).toBe(f.threes![1]);
    expect(text).toContain("Level on live threes");
    expect(text).toContain("a drand coin flip decided");
    expect(text).toContain(v.result!.winner!);
  });
});

describe("a live game", () => {
  it("before the beacon deals: no opening, no board, the round to wait for", () => {
    const v = view(pre), d = data(pre);
    expect(v.opening).toBeNull();
    expect(v.moves).toEqual([]);
    expect(v.toMove).toBeNull();
    expect(v.gate).toBeNull();
    expect(v.nameRound).toBe(d.rand.round);
    const f = spillwayFrameAt(v, 0, false);
    expect(f.dealt).toBe(false);
    expect(f.next).toBeNull();
  });

  it("with the next gate not drawn yet: gate_ready is false, no gate, nothing open", () => {
    const v = view(mid), d = data(mid);
    expect(d.gate_ready).toBe(false);
    expect(v.gateReady).toBe(false);
    expect(v.gate).toBeNull();
    expect(v.open).toEqual([]);
    expect(v.gateAt).toBe(d.gate_at);
    expect(v.toMove).toBe(d.to_move);
    expect(v.gateRound).toBe(d.gate_round);
    expect(rows(boardAt(v, v.moves.length))).toEqual(d.board);
    expect(v.result).toBeNull();
    const f = spillwayFrameAt(v, v.moves.length, false);
    expect(f.next?.toMove).toBe(d.to_move);
    expect(f.next?.gate).toBeNull();
    expect(v.secondsLeft).not.toBeNull();
  });

  it("a gate that is drawn is read from the flat state", () => {
    const d = data(mid);
    const drawn = { ...session(mid), state: { data: { ...d, gate_ready: true, gate: 3, open: [1, 2, 4, 5, 6] } } } as unknown as Session;
    const v = spillwayView(drawn)!;
    expect(v.gateReady).toBe(true);
    expect(v.gate).toBe(3);
    expect(v.open).toEqual([1, 2, 4, 5, 6]);
    expect(spillwayFrameAt(v, v.moves.length, false).next).toEqual({ toMove: d.to_move, gate: 3, round: d.gate_round });
  });
});
