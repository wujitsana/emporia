/**
 * Sluice (`emporia:sluice:v1`), the four-in-a-line game, as a viewer sees it — pure: a session (and its
 * actions) in, what to draw out. Drawn by ui/SluicePanel.tsx and, step by step, by the Arena.
 *
 * This file is the one place that knows the wire (the contract is section 2.5 of the games plan): if the
 * module reports a difference, change the readers here and nothing else. The board at any step is rebuilt
 * from `opening` and `moves`; no hashing, no randomness, no secret is read. Visibility only: the dashboard
 * never drops a stone. Cells are `[channel, row]`: channel 1-6 left to right, row 1-5 from the bottom.
 */
import type { Session, SessionAction } from "./api";
import { stateData } from "./setPlay";

export const SLUICE = "emporia:sluice:v1";
export const isSluice = (m: string | undefined | null) => !!m && (m ?? "").replace(/:v\d+$/, "") === SLUICE.replace(/:v\d+$/, "");

export type Cell = [channel: number, row: number];

export interface SluiceMove {
  n: number;
  by: string;
  channel: number;
  row: number | null;
  /** The channel the beacon shut for this drop; null when only one channel was open. */
  gate: number | null;
  round: number | null;
}

export interface SluiceOpening {
  silt: Cell[];
  /** Stones per seat, in seat order. */
  stones: [Cell[], Cell[]];
  first: string | null;
}

export interface SluiceResult {
  winner: string | null;
  /** `four` | `threes` | `level` | `timeout` | `void` */
  termination: string;
  line: Cell[] | null;
  /** Live threes per seat id, when the result carries them. */
  threes: Record<string, number> | null;
  points: Record<string, number> | null;
}

export interface SluiceView {
  kind: "sluice";
  cols: number;
  rows: number;
  drops: number;
  /** Seat A then seat B. */
  seats: string[];
  opening: SluiceOpening | null;
  moves: SluiceMove[];
  toMove: string | null;
  gate: number | null;
  open: number[];
  gateRound: number | null;
  /** The drand round that deals the opening, while it is not dealt yet. */
  nameRound: number | null;
  result: SluiceResult | null;
  secondsLeft: number | null;
}

type Data = Record<string, unknown>;
const obj = (v: unknown): Data => (v && typeof v === "object" && !Array.isArray(v) ? (v as Data) : {});
const int = (v: unknown): number | null => (typeof v === "number" && Number.isInteger(v) ? v : null);
const num = (v: unknown, fallback: number): number => (typeof v === "number" && Number.isFinite(v) ? v : fallback);

function cell(v: unknown): Cell | null {
  if (Array.isArray(v) && v.length === 2 && int(v[0]) !== null && int(v[1]) !== null) return [v[0] as number, v[1] as number];
  const o = obj(v);
  return int(o.channel) !== null && int(o.row) !== null ? [o.channel as number, o.row as number] : null;
}
const cells = (v: unknown): Cell[] => (Array.isArray(v) ? v.flatMap((c) => { const x = cell(c); return x ? [x] : []; }) : []);

function readOpening(v: unknown, seats: string[]): SluiceOpening | null {
  const o = obj(v);
  if (!Object.keys(o).length) return null;
  const st = obj(o.stones);
  return {
    silt: cells(o.silt),
    stones: [cells(st[seats[0]] ?? st.A), cells(st[seats[1]] ?? st.B)],
    first: typeof o.first === "string" ? o.first : null,
  };
}

function readMoves(v: unknown): SluiceMove[] {
  return (Array.isArray(v) ? v : []).flatMap((m, k) => {
    const o = obj(m);
    const channel = int(o.channel);
    if (channel === null || typeof o.by !== "string") return [];
    return [{ n: int(o.n) ?? k + 1, by: o.by, channel, row: int(o.row), gate: int(o.gate), round: int(o.round) }];
  });
}

function readResult(actions?: SessionAction[] | null): SluiceResult | null {
  for (const a of [...(actions ?? [])].reverse()) {
    const r = (a.result?.artifacts as { result?: Data } | undefined)?.result;
    if (!r || typeof r !== "object") continue;
    const pts = obj(r.points);
    const th = obj(r.threes);
    return {
      winner: typeof r.winner === "string" ? r.winner : null,
      termination: typeof r.termination === "string" ? r.termination : "",
      line: Array.isArray(r.line) && r.line.length ? cells(r.line) : null,
      threes: Object.keys(th).length ? Object.fromEntries(Object.entries(th).map(([k, n]) => [k, num(n, 0)])) : null,
      points: Object.keys(pts).length ? Object.fromEntries(Object.entries(pts).map(([k, n]) => [k, num(n, 0)])) : null,
    };
  }
  return null;
}

/** Pure: a Sluice session (and its actions) in, what to draw out; null for any other game. */
export function sluiceView(session: Session, actions?: SessionAction[] | null, now: number = Date.now()): SluiceView | null {
  if (!isSluice(session.module_type)) return null;
  const d = stateData(session);
  const st = obj(session.state);
  const dv = obj(d.derived ?? st.derived);
  const seats = Array.isArray(d.seats) && d.seats.length === 2 ? (d.seats as string[]) : (session.participants ?? []).slice(0, 2);
  const moves = readMoves(d.moves);
  const limit = num(d.move_time_limit_s, 0);
  const at = Date.parse(String(dv.gate_at ?? d.turn_started_at ?? ""));
  const ended = d.ended !== null && d.ended !== undefined;
  const opening = readOpening(d.opening ?? dv.opening, seats);
  return {
    kind: "sluice",
    cols: num(d.cols, 6),
    rows: num(d.rows, 5),
    drops: num(d.drops, 7),
    seats,
    opening,
    moves,
    toMove: typeof dv.to_move === "string" ? dv.to_move : null,
    gate: int(dv.gate),
    open: Array.isArray(dv.open) ? (dv.open as unknown[]).flatMap((c) => (int(c) !== null ? [c as number] : [])) : [],
    gateRound: int(dv.gate_round),
    nameRound: int(dv.name_round) ?? int(obj(d.rand).round),
    result: readResult(actions),
    secondsLeft: session.status === "active" && !ended && limit && Number.isFinite(at) ? Math.max(0, Math.round(limit - (now - at) / 1000)) : null,
  };
}

/** One mark per cell: `.` empty, `#` silt, `A`, `B`. Rows top first, as the relay's `board`. */
export type Board = string[][];

/** Rebuild the board after the first `upTo` moves. */
export function boardAt(v: Pick<SluiceView, "cols" | "rows" | "seats" | "opening" | "moves">, upTo: number): Board {
  const g: Board = Array.from({ length: v.rows }, () => Array.from({ length: v.cols }, () => "."));
  const put = ([c, r]: Cell, mark: string) => { if (c >= 1 && c <= v.cols && r >= 1 && r <= v.rows) g[v.rows - r][c - 1] = mark; };
  v.opening?.silt.forEach((c) => put(c, "#"));
  v.opening?.stones[0].forEach((c) => put(c, "A"));
  v.opening?.stones[1].forEach((c) => put(c, "B"));
  for (const m of v.moves.slice(0, upTo)) {
    const mark = v.seats.indexOf(m.by) === 1 ? "B" : "A";
    let row = m.row;
    if (row === null) { // the stone settles on what is below it
      row = 1;
      while (row <= v.rows && g[v.rows - row][m.channel - 1] !== ".") row += 1;
    }
    put([m.channel, row], mark);
  }
  return g;
}

const DIRS: [number, number][] = [[0, 1], [1, 0], [1, 1], [1, -1]];

/** Every window of four cells in a line, as `[channel, row]`. */
function windows(b: Board): Cell[][] {
  const rows = b.length;
  const cols = b[0]?.length ?? 0;
  const out: Cell[][] = [];
  for (let r = 0; r < rows; r++) for (let c = 0; c < cols; c++) for (const [dr, dc] of DIRS) {
    const w: Cell[] = [];
    for (let k = 0; k < 4; k++) {
      const rr = r + dr * k, cc = c + dc * k;
      if (rr < 0 || rr >= rows || cc < 0 || cc >= cols) break;
      w.push([cc + 1, rows - rr]);
    }
    if (w.length === 4) out.push(w);
  }
  return out;
}
const markAt = (b: Board, [c, r]: Cell) => b[b.length - r][c - 1];

/** The four cells of a seat's line, if it has one. */
export function findFour(b: Board): { mark: string; line: Cell[] } | null {
  for (const w of windows(b)) {
    const m = markAt(b, w[0]);
    if ((m === "A" || m === "B") && w.every((c) => markAt(b, c) === m)) return { mark: m, line: [...w].sort((a, b) => a[0] - b[0] || a[1] - b[1]) };
  }
  return null;
}

/** Live threes per seat: a line of four cells with three of its stones and one empty cell. */
export function liveThrees(b: Board): [number, number] {
  const t: [number, number] = [0, 0];
  for (const w of windows(b)) {
    const ms = w.map((c) => markAt(b, c));
    if (ms.filter((x) => x === ".").length !== 1) continue;
    (["A", "B"] as const).forEach((s, i) => { if (ms.filter((x) => x === s).length === 3) t[i] += 1; });
  }
  return t;
}

export interface SluiceFrame {
  kind: "sluice";
  seats: string[];
  cols: number;
  rows: number;
  drops: number;
  /** False before the beacon deals the opening: no board yet. */
  dealt: boolean;
  nameRound: number | null;
  board: Board;
  /** Moves applied; 0 is the opening as dealt. */
  step: number;
  /** The moves applied so far, and the last of them. */
  moves: SluiceMove[];
  last: SluiceMove | null;
  /** The channel shut for the drop that made this board; null when none was. */
  gate: number | null;
  /** Live game, board is current: whose drop is next, the channel shut for it and the round to name. */
  next: { toMove: string | null; gate: number | null; round: number | null } | null;
  /** Drops left per seat. */
  left: [number, number];
  /** Only on the last step of a finished game. */
  line: Cell[] | null;
  threes: [number, number] | null;
  result: SluiceResult | null;
  /** Who moved first, and so who wins a level count. */
  first: string | null;
}

/** The table after the first `upTo` moves. `atEnd` is the last step of a finished game (the verdict shows). */
export function sluiceFrameAt(v: SluiceView, upTo: number, atEnd: boolean): SluiceFrame {
  const step = Math.max(0, Math.min(v.moves.length, upTo));
  const board = boardAt(v, step);
  const last = step ? v.moves[step - 1] : null;
  const used: [number, number] = [0, 0];
  v.moves.slice(0, step).forEach((m) => { used[v.seats.indexOf(m.by) === 1 ? 1 : 0] += 1; });
  const result = atEnd ? v.result : null;
  const term = result?.termination;
  const four = result && term === "four" ? (result.line ?? findFour(board)?.line ?? null) : null;
  let threes: [number, number] | null = null;
  if (result && (term === "threes" || term === "level")) {
    const computed = liveThrees(board);
    threes = result.threes
      ? [result.threes[v.seats[0]] ?? result.threes.A ?? computed[0], result.threes[v.seats[1]] ?? result.threes.B ?? computed[1]]
      : computed;
  }
  const live = !result && step === v.moves.length;
  return {
    kind: "sluice", seats: v.seats, cols: v.cols, rows: v.rows, drops: v.drops,
    dealt: !!v.opening, nameRound: v.nameRound, board, step, moves: v.moves.slice(0, step), last,
    gate: last?.gate ?? null,
    next: live && v.opening ? { toMove: v.toMove, gate: v.gate, round: v.gateRound } : null,
    left: [Math.max(0, v.drops - used[0]), Math.max(0, v.drops - used[1])],
    line: four, threes, result, first: v.opening?.first ?? null,
  };
}

/** One line for a drop step: "dropped in channel 3, row 2; channel 5 was gated". */
export function dropText(m: SluiceMove): string {
  return `dropped in channel ${m.channel}${m.row ? `, row ${m.row}` : ""}${m.gate ? `; channel ${m.gate} was gated` : ""}`;
}

/** "alpha wins with four in a line", "alpha wins on live threes (2 to 1)", … in words, from a result. */
export function sluiceVerdict(f: Pick<SluiceFrame, "seats" | "threes" | "first">, r: SluiceResult, name: (id: string) => string): string {
  const w = r.winner ? name(r.winner) : null;
  switch (r.termination) {
    case "four": return `${w ?? "nobody"} wins with four in a line`;
    case "threes": return `${w ?? "nobody"} wins on live threes${f.threes ? ` (${name(f.seats[0])} ${f.threes[0]}, ${name(f.seats[1])} ${f.threes[1]})` : ""}`;
    case "level": return `Level on live threes${f.threes ? ` (${f.threes[0]} each)` : ""}: ${w ?? "the player who moved second"} moved second and wins`;
    case "timeout": return `${w ?? "nobody"} wins: the other ran out of time`;
    case "void": return "The game was voided";
    default: return w ? `${w} wins` : "No winner";
  }
}

export const cellText = ([c, r]: Cell) => `channel ${c}, row ${r}`;
