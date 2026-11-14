import type { TesseractFrame } from "../../tesseract";
import { AXES, pushText, tesseractVerdict } from "../../tesseract";
import type { TesseractBoardProps } from "./TesseractBoard";

type Name = (id: string) => string;

/** The Arena frame as the board's props: names shortened, pushes as words, the verdict as one line. */
export function tesseractFromFrame(f: TesseractFrame, name: Name): TesseractBoardProps {
  const r = f.result;
  return {
    epoch: f.epoch, epochs: f.epochs, phase: f.phase,
    seats: f.seats.map((s) => ({ name: name(s.agent), state: s.state, winner: !!r && (r.winner === s.agent || r.tied.includes(s.agent)) })),
    opening: f.opening,
    rounds: f.rounds.map((x) => ({
      epoch: x.epoch, mix: x.mix, dark: x.dark,
      pushes: Object.entries(x.pushes).map(([a, p]) => ({ name: name(a), text: pushText(p) })),
      silent: x.silent.map(name), late: x.late.map(name),
    })),
    verdict: r ? tesseractVerdict(r, name) : null,
    results: r ? r.order.map((a, k) => ({ place: k + 1, name: name(a), total: r.points[a] ?? 0, index: r.tableIndex[a] ?? 0,
      target: r.targets[a] ? AXES.map((ax, j) => `${ax} ${r.targets[a][j]}`).join(" ") : null })) : null,
  };
}
