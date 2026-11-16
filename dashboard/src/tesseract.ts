/**
 * Tesseract (`emporia:tesseract:v1`), the table game on the edges of a four-dimensional cube, as a viewer sees it:
 * pure, a session and its actions in, what to draw out. Drawn, step by step, by the Arena (ui/boards/TesseractBoard).
 *
 * Visibility only. Everything here is public in the record: who sealed (never what), the pushes once opened, each
 * round's mix, and, once the table is over, the totals and the targets. A commitment, a nonce or a secret is never
 * read. The scores are not recomputed here (the full view, with its own rules in TypeScript pinned to
 * tesseract.vectors.json, is T2): the totals shown are the ones the relay's result carries.
 */
import type { Session, SessionAction } from "./api";
import { stateData } from "./setPlay";

export const TESSERACT = "emporia:tesseract:v1";
export const isTesseract = (m: string | undefined | null) => !!m && m.replace(/:v\d+$/, "") === TESSERACT.replace(/:v\d+$/, "");
export const AXES = ["W", "X", "Y", "Z"] as const;
export const EPOCHS = 5;

/** The board is the edge set of a four-dimensional cube: a corner is a subset of W X Y Z. Same order as the relay's
 * `tesseract_core.topology()` (pinned to the vectors in the tests): inputs, hubs, outputs; channels input-to-hub first. */
export const INPUTS = ["XYZ", "WYZ", "WXZ", "WXY"] as const;
export const HUBS = ["WX", "WY", "WZ", "XY", "XZ", "YZ"] as const;
export const OUTPUTS = ["W", "X", "Y", "Z"] as const;
export const CHANNELS: readonly string[] = [
  ...INPUTS.flatMap((i) => HUBS.filter((h) => [...h].every((c) => i.includes(c))).map((h) => `${i}>${h}`)),
  ...HUBS.flatMap((h) => OUTPUTS.filter((o) => h.includes(o)).map((o) => `${h}>${o}`)),
];
export type Pull = Record<string, { up: number[]; down: number[] }>;

export type TesseractSeatState = "waiting" | "sealed" | "opened" | "out";
export interface TesseractRound {
  epoch: number;
  /** Basis points per output, W X Y Z (the four add to just under 10000). */
  mix: number[];
  /** Who pushed what, once opened: {agent: {channel: amount}}. */
  pushes: Record<string, Record<string, number>>;
  dark: number;
  /** The dark channels' ids (dark for this round only). */
  darkIds: string[];
  /** The 24 widths after this round's pushes, in channel order. */
  widths: number[];
  /** Seated players who sealed nothing this round, and those put out in it. */
  silent: string[];
  late: string[];
}
export interface TesseractResult {
  winner: string | null;
  tied: string[];
  points: Record<string, number>;
  tableIndex: Record<string, number>;
  order: string[];
  out: string[];
  unrevealed: string[];
  targets: Record<string, number[]>;
}
export interface TesseractFrame {
  kind: "tesseract";
  epoch: number;
  epochs: number;
  phase: "seal" | "open" | "secrets" | "done";
  players: string[];
  seats: { agent: string; state: TesseractSeatState }[];
  opening: number[] | null;
  rounds: TesseractRound[];
  secrets: string[];
  result: TesseractResult | null;
  /** A practice table: it counts for nothing (no standing, XP, badges or quests). */
  practice: boolean;
  /** The opening network's widths (public from the start), the widest a channel can be, and, only when the frame is the
   * table as it stands now, the pull table the relay serves (an output's change in basis points for +4 / -4). */
  openingWidths: number[] | null;
  widthMax: number;
  pull: Pull | null;
}

type Rec = Record<string, unknown>;
const obj = (v: unknown): Rec => (v && typeof v === "object" && !Array.isArray(v) ? (v as Rec) : {});
const nums = (v: unknown): number[] => (Array.isArray(v) ? v.filter((x): x is number => typeof x === "number") : []);

/** The relay's result for the table, from the action that ended it (absent for a table the clock ended). */
export function tesseractResult(actions: SessionAction[] | null | undefined): TesseractResult | null {
  for (const a of [...(actions ?? [])].reverse()) {
    const r = obj(obj(a.result?.artifacts).result);
    if (Object.keys(r).length && r.points) {
      return {
        winner: typeof r.winner === "string" ? r.winner : null,
        tied: Array.isArray(r.tied) ? (r.tied as string[]) : [],
        points: obj(r.points) as Record<string, number>,
        tableIndex: obj(r.table_index) as Record<string, number>,
        order: Array.isArray(r.order) ? (r.order as string[]) : [],
        out: Array.isArray(r.out) ? (r.out as string[]) : [],
        unrevealed: Array.isArray(r.unrevealed) ? (r.unrevealed as string[]) : [],
        targets: obj(r.targets) as Record<string, number[]>,
      };
    }
  }
  return null;
}

/** "WX>W +4, YZ>Z −3": a push as words (an empty push says so). */
export function pushText(push: Record<string, number> | undefined): string {
  const e = Object.entries(push ?? {}).filter(([, n]) => n);
  return e.length ? e.map(([c, n]) => `${c} ${n > 0 ? "+" : "−"}${Math.abs(n)}`).join(", ") : "nothing";
}

function published(session: Session): { opening: number[] | null; openingWidths: number[] | null; rounds: Map<number, TesseractRound> } {
  const pub = obj(stateData(session).published);
  const rounds = new Map<number, TesseractRound>();
  for (const [k, v] of Object.entries(pub)) {
    const r = obj(v);
    if (k === "opening" || !Number.isInteger(Number(k)) || !Array.isArray(r.mix)) continue;
    rounds.set(Number(k), {
      epoch: Number(k), mix: nums(r.mix), pushes: obj(r.pushes) as TesseractRound["pushes"],
      dark: Array.isArray(r.dark) ? r.dark.length : 0,
      darkIds: Array.isArray(r.dark) ? (r.dark as string[]) : [], widths: nums(r.widths),
      silent: Array.isArray(r.silent) ? (r.silent as string[]) : [], late: Array.isArray(r.late) ? (r.late as string[]) : [],
    });
  }
  const o = obj(pub.opening);
  return { opening: Array.isArray(o.mix) ? nums(o.mix) : null, openingWidths: Array.isArray(o.widths) ? nums(o.widths) : null, rounds };
}

const board = (session: Session): Rec => obj(stateData(session).board);
function widthMax(session: Session): number {
  const w = board(session).limits;
  const hi = Array.isArray(obj(w).width) ? (obj(w).width as unknown[])[1] : undefined;
  return typeof hi === "number" && hi > 0 ? hi : 20;
}
/** The relay's pull table, only when `shown` rounds are all the rounds it has resolved: it describes the network as it
 * stands now, so a step back in a replay must not show it. */
function pullNow(session: Session, shown: number): Pull | null {
  const b = board(session);
  const pull = obj(b.pull);
  if (!Object.keys(pull).length || !Array.isArray(b.done) || b.done.length !== shown) return null;
  const out: Pull = {};
  for (const [c, row] of Object.entries(pull)) out[c] = { up: nums(obj(row).up), down: nums(obj(row).down) };
  return out;
}

/** The table after the first `i` actions (`acts` are those that took effect), `finished`: the table is over. */
export function tesseractFrameAt(session: Session, acts: SessionAction[], i: number, finished: boolean): TesseractFrame {
  const players = session.participants ?? [];
  const { opening, openingWidths, rounds } = published(session);
  const sealed = new Map<number, Set<string>>();
  const opened = new Map<number, Map<string, Record<string, number>>>();
  const secrets: string[] = [];
  for (const a of acts.slice(0, i)) {
    const p = obj(a.payload);
    const e = typeof p.epoch === "number" ? p.epoch : 0;
    if (a.action_type === "seal") {
      if (!sealed.has(e)) sealed.set(e, new Set());
      sealed.get(e)!.add(a.agent_id);
    } else if (a.action_type === "open") {
      if (!opened.has(e)) opened.set(e, new Map());
      opened.get(e)!.set(a.agent_id, obj(obj(p.move).push) as Record<string, number>);
    } else if (a.action_type === "reveal" && "s" in p && !secrets.includes(a.agent_id)) secrets.push(a.agent_id);
  }
  const atEnd = finished && i === acts.length;
  const result = atEnd ? tesseractResult(acts) : null;
  const seen = [...sealed.keys(), ...opened.keys()].filter((e) => e > 0);
  let epoch = seen.length ? Math.max(...seen) : 1;
  const sealedNow = sealed.get(epoch)?.size ?? 0;
  const openedNow = opened.get(epoch)?.size ?? 0;
  const done = (n: number) => rounds.has(n) && (opened.get(n)?.size ?? 0) >= (sealed.get(n)?.size ?? 0) && (sealed.get(n)?.size ?? 0) > 0;
  let phase: TesseractFrame["phase"] = sealedNow >= players.length && sealedNow > openedNow ? "open" : "seal";
  if (sealedNow > 0 && openedNow >= sealedNow && sealedNow >= players.length) {
    if (epoch >= EPOCHS) phase = "secrets";
    else { epoch += 1; phase = "seal"; }
  }
  if (atEnd) phase = "done";
  const shown = [...rounds.values()].filter((r) => atEnd || done(r.epoch)).sort((a, b) => a.epoch - b.epoch).map((r) => {
    const o = opened.get(r.epoch);
    // pushes are public once opened: what the record says, or, a step before it is published, what was opened
    return atEnd || !o ? r : { ...r, pushes: Object.fromEntries([...o].filter(([, p]) => Object.keys(p).length)) };
  });
  const out = new Set(result?.out ?? []);
  const seats = players.map((agent) => {
    let state: TesseractSeatState = "waiting";
    if (out.has(agent)) state = "out";
    else if (phase === "seal") state = sealed.get(epoch)?.has(agent) ? "sealed" : "waiting";
    else if (phase === "open") state = opened.get(epoch)?.has(agent) ? "opened" : sealed.get(epoch)?.has(agent) ? "sealed" : "waiting";
    else state = secrets.includes(agent) || phase === "done" ? "opened" : "waiting";
    return { agent, state };
  });
  return { kind: "tesseract", epoch: Math.min(epoch, EPOCHS), epochs: EPOCHS, phase, players, seats, opening, rounds: shown, secrets, result,
    practice: session.config?.practice === true || obj(obj(session.state).data).practice === true,
    openingWidths, widthMax: widthMax(session), pull: pullNow(session, shown.length) };
}

/** One line for the verdict, in words. */
export function tesseractVerdict(r: TesseractResult, name: (id: string) => string = (s) => s): string {
  if (r.winner) return `${name(r.winner)} won with ${r.points[r.winner] ?? 0} points`;
  if (r.tied.length) return `${r.tied.map(name).join(" and ")} tied at ${r.points[r.tied[0]] ?? 0} points: a draw`;
  return "no one finished: a draw";
}
