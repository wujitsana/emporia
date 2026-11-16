/**
 * The Tesseract board view (T2): what to draw of a table, from the Arena's frame. Pure, no DOM. The frame already holds
 * only what the record shows at that step (who sealed, never what; pushes only once opened; targets only at the end),
 * so nothing here can show a secret early: it never reads a commitment, a nonce or a secret.
 *
 * Geometry. A corner is a subset of W X Y Z; in four dimensions it sits at +1/2 on each letter it has and -1/2 on the
 * others. The four outputs are the four axes: output W is (1/2, -1/2, -1/2, -1/2), and the mix, four shares adding to 1,
 * is the point whose coordinates are the shares minus 1/2. It therefore stays inside the tetrahedron spanned by the
 * four outputs (a 3-simplex in the 4-cube). Lengths are foreshortened by the projection, so a distance to a target is
 * printed as a number (the score's four-gap sum), never read off the picture.
 */
import { CHANNELS, HUBS, INPUTS, OUTPUTS, type TesseractFrame } from "./tesseract";
import { rotate4, to3, type Vec4 } from "./ui/graph3d/project4";
import type { Vec3 } from "./ui/graph3d/project";

export const ALL = "all";
const LETTERS = ["W", "X", "Y", "Z"] as const;

export interface ChannelRow {
  id: string;
  from: string;
  to: string;
  width: number;
  /** Net push this round by everyone (or by the one player chosen); 0 in the opening. */
  net: number;
  /** Who pushed it this round: public once opened. */
  pushes: { agent: string; amount: number }[];
  dark: boolean;
  /** Each output's change in basis points for +4 / -4 on this channel; only for the table as it stands now. */
  pull: { up: number[]; down: number[] } | null;
}
export interface BoardState {
  /** 0 is the opening network, k the network after round k. */
  step: number;
  label: string;
  mix: number[];
  channels: ChannelRow[];
  darkCount: number;
}

/** The networks the record shows so far: the opening (when dealt), then one per resolved round. */
export function boardStates(f: TesseractFrame, only: string = ALL): BoardState[] {
  const out: BoardState[] = [];
  const mk = (step: number, label: string, widths: number[], mix: number[], dark: string[], pushes: Record<string, Record<string, number>>, live: boolean) => {
    const channels = CHANNELS.map((id, k): ChannelRow => {
      const [from, to] = id.split(">");
      const by = Object.entries(pushes).map(([agent, p]) => ({ agent, amount: p[id] ?? 0 })).filter((x) => x.amount !== 0);
      const net = by.filter((x) => only === ALL || x.agent === only).reduce((s, x) => s + x.amount, 0);
      return { id, from, to, width: widths[k] ?? 0, net, pushes: by, dark: dark.includes(id), pull: live ? (f.pull?.[id] ?? null) : null };
    });
    out.push({ step, label, mix, channels, darkCount: dark.length });
  };
  if (f.openingWidths?.length === CHANNELS.length && f.opening) mk(0, "opening", f.openingWidths, f.opening, [], {}, f.rounds.length === 0);
  f.rounds.forEach((r, k) => {
    if (r.widths.length === CHANNELS.length) mk(r.epoch, `round ${r.epoch}`, r.widths, r.mix, r.darkIds, r.pushes, k === f.rounds.length - 1);
  });
  return out;
}

/** True once anyone's pushes are public (an opened round): only then may the view offer one player's pushes. */
export const ownersPublic = (f: TesseractFrame) => f.rounds.some((r) => Object.keys(r.pushes).length > 0);

/** The score's distance: the sum over the four outputs of |mix (bp) - 100 * target (percent)|. */
export function distance(mix: readonly number[], target: readonly number[]): number {
  return LETTERS.reduce((s, _, k) => s + Math.abs((mix[k] ?? 0) - 100 * (target[k] ?? 0)), 0);
}

/** Targets are known only at the end (the relay's result). Before that: none, whoever asks. */
export function targetsKnown(f: TesseractFrame): Record<string, number[]> {
  return f.phase === "done" && f.result ? f.result.targets : {};
}

/** One line saying what the view shows at this phase and what it does not (yet). */
export function phaseNote(f: TesseractFrame): string {
  if (f.phase === "done") return "The table is over: every target is shown.";
  if (f.phase === "secrets") return "All five rounds are played; secrets are being revealed. Targets stay hidden until the end.";
  if (f.phase === "open") return `Round ${f.epoch}: everyone has sealed a push and the pushes are being opened. A round shows its pushes once all are opened.`;
  return `Round ${f.epoch}: pushes are sealed, not shown. A round shows its pushes once everyone has opened.`;
}

// -- the corners in four dimensions ----------------------------------------------------------------------------

/** A corner (a subset of W X Y Z, "" for the drain, "WXYZ" for the pulse) as a point of the 4-cube centered on 0. */
export const corner4 = (name: string): Vec4 => [0, 1, 2, 3].map((k) => (name.includes(LETTERS[k]) ? 0.5 : -0.5)) as unknown as Vec4;
/** The mix as a point of the same space: shares minus 1/2. The relay's four floors add to just under 10000, so the
 * shares are taken over their own sum: the point is then exactly inside the outputs' tetrahedron. */
export const mix4 = (mix: readonly number[]): Vec4 => {
  const sum = [0, 1, 2, 3].reduce((s, k) => s + (mix[k] ?? 0), 0) || 10000;
  return [0, 1, 2, 3].map((k) => (mix[k] ?? 0) / sum - 0.5) as unknown as Vec4;
};
/** A target (percent) as a point of the same space. */
export const target4 = (t: readonly number[]): Vec4 => [0, 1, 2, 3].map((k) => (t[k] ?? 0) / 100 - 0.5) as unknown as Vec4;

/** Everything is scaled so the figure fits the kit's unit sphere at zoom 1. */
const FIT = 0.8;
export function place3(p: Vec4, a: number, b: number): Vec3 {
  const q = to3(rotate4(p, a, b), 3);
  return [q[0] * FIT, q[1] * FIT, q[2] * FIT];
}

/** The 15 role corners (and the drain) at an angle: the node positions of the 3D view. */
export function cornerPositions(a: number, b: number): Map<string, Vec3> {
  return new Map([...INPUTS, ...HUBS, ...OUTPUTS].map((c) => [c, place3(corner4(c), a, b)] as const));
}

export const PULSE = "WXYZ";
export const DRAIN = "";
