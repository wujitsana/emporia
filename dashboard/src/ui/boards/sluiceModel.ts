import type { SluiceFrame, SluiceView } from "../../sluice";
import { sluiceFrameAt, sluiceVerdict } from "../../sluice";
import type { SluiceBoardProps } from "./SluiceBoard";

/**
 * View models for the Sluice board, built from a live view (the Games panel) or from an Arena frame, so
 * both draw the same thing. Everything Sluice shows is public in the record; nothing is hidden here.
 */
type Name = (id: string) => string;

export function sluiceFromFrame(f: SluiceFrame, name: Name): SluiceBoardProps {
  return {
    cols: f.cols, rows: f.rows,
    seats: [name(f.seats[0] ?? "A"), name(f.seats[1] ?? "B")],
    dealt: f.dealt, nameRound: f.nameRound, board: f.board,
    moves: f.moves.map((m) => ({ n: m.n, name: name(m.by), channel: m.channel, row: m.row, gate: m.gate })),
    last: f.last ? { n: f.last.n, name: name(f.last.by), channel: f.last.channel, row: f.last.row } : null,
    gate: f.next?.gate ? { channel: f.next.gate, now: true } : f.gate ? { channel: f.gate, now: false } : null,
    line: f.line, threes: f.threes, left: f.left,
    verdict: f.result ? sluiceVerdict(f, f.result, name) : null,
  };
}

/** The Games panel: the board as it stands now (or at the end, with the verdict). */
export function sluiceFromView(v: SluiceView, name: Name = (s) => s): SluiceBoardProps {
  return sluiceFromFrame(sluiceFrameAt(v, v.moves.length, !!v.result), name);
}
