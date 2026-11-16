import type { SpillwayFrame, SpillwayView } from "../../spillway";
import { spillwayFrameAt, spillwayVerdict } from "../../spillway";
import type { SpillwayBoardProps } from "./SpillwayBoard";

/**
 * View models for the Spillway board, built from a live view (the Games panel) or from an Arena frame, so
 * both draw the same thing. Everything Spillway shows is public in the record; nothing is hidden here.
 */
type Name = (id: string) => string;

export function spillwayFromFrame(f: SpillwayFrame, name: Name): SpillwayBoardProps {
  return {
    cols: f.cols, rows: f.rows,
    seats: [name(f.seats[0] ?? "A"), name(f.seats[1] ?? "B")],
    dealt: f.dealt, nameRound: f.nameRound, board: f.board,
    moves: f.moves.map((m) => ({ n: m.n, name: name(m.by), channel: m.channel, row: m.row, gate: m.gate })),
    last: f.last ? { n: f.last.n, name: name(f.last.by), channel: f.last.channel, row: f.last.row } : null,
    gate: f.next?.gate ? { channel: f.next.gate, now: true } : f.gate ? { channel: f.gate, now: false } : null,
    line: f.line, threes: f.threes, left: f.left,
    verdict: f.result ? spillwayVerdict(f, f.result, name) : null,
  };
}

/** The Games panel: the board as it stands now (or at the end, with the verdict). */
export function spillwayFromView(v: SpillwayView, name: Name = (s) => s): SpillwayBoardProps {
  return spillwayFromFrame(spillwayFrameAt(v, v.moves.length, !!v.result), name);
}
