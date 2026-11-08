/**
 * The games pack (`emporia:liars-dice:v1`, `emporia:cv-auction:v1`) as a viewer sees it — pure: a
 * session (and its actions) in, what to draw out. Drawn by ui/ChancePanel.tsx.
 *
 * Both draw from `emporia:rand:v1`: nobody deals a die or a signal, so the relay holds none while
 * the game runs and neither does this view. Once the game is over the relay publishes what the
 * players revealed (`state.data.deal.hands[p].values`: the dice, or the signals), recomputed from
 * their secrets and the beacon. Visibility only: the dashboard bids and calls nothing.
 */
import type { Session, SessionAction } from "./api";
import { stateData } from "./setPlay";

export const LIARS_DICE = "emporia:liars-dice:v1";
export const CV_AUCTION = "emporia:cv-auction:v1";

const base = (m: string | undefined | null) => (m ?? "").replace(/:v\d+$/, "");
export const isLiarsDice = (m: string | undefined | null) => !!m && base(m) === base(LIARS_DICE);
export const isCvAuction = (m: string | undefined | null) => !!m && base(m) === base(CV_AUCTION);
/** The games pack: free, zero-sum, and counted as set plays. */
export const isChanceGame = (m: string | undefined | null) => isLiarsDice(m) || isCvAuction(m);

export interface ChanceResult {
  winner: string | null;
  termination?: string;
  reason?: string;
  points?: Record<string, number>;
  [k: string]: unknown;
}

export interface Bid { n: number; by: string; quantity: number; face: number }

export interface DiceView {
  kind: "dice";
  players: number;
  diceEach: number;
  onesWild: boolean;
  maxBids: number;
  /** Who bids first, then who: the beacon's order (public from the round on); [] until it is read. */
  order: string[];
  bids: Bid[];
  /** The call that ended the play, once there is one. */
  call: { by: string; bid: number; bidder: string } | null;
  /** Who has revealed its secret. */
  revealed: string[];
  /** Each player's dice, published once the game is over. */
  dice: Record<string, number[]>;
  toMove: string | null;
  result: ChanceResult | null;
  secondsLeft: number | null;
}

export interface AuctionView {
  kind: "auction";
  players: number;
  spread: number;
  phase: "commit" | "reveal" | "secrets";
  /** Who has sealed a bid / revealed it / revealed its secret. The sealed bids themselves are hashes. */
  committed: string[];
  bids: Record<string, number>;
  secrets: string[];
  /** The public base value B (from the beacon), once the round is read. */
  base: number | null;
  /** Each player's signal, published once the game is over. */
  signals: Record<string, number>;
  result: ChanceResult | null;
  secondsLeft: number | null;
}

export type ChanceView = DiceView | AuctionView;

type Data = Record<string, unknown>;
const obj = <T>(v: unknown): Record<string, T> => (v && typeof v === "object" && !Array.isArray(v) ? (v as Record<string, T>) : {});
const num = (v: unknown, fallback: number): number => (typeof v === "number" && Number.isFinite(v) ? v : fallback);

function verdict(actions?: SessionAction[] | null): ChanceResult | null {
  for (const a of [...(actions ?? [])].reverse()) {
    const r = (a.result?.artifacts as { result?: ChanceResult } | undefined)?.result;
    if (r) return r;
  }
  return null;
}

function secondsLeft(session: Session, d: Data, field: string, now: number): number | null {
  const limit = num(d.move_time_limit_s, 0);
  const at = typeof d[field] === "string" ? Date.parse(d[field] as string) : NaN;
  return session.status === "active" && limit && Number.isFinite(at) ? Math.max(0, Math.round(limit - (now - at) / 1000)) : null;
}

/** Pure: a games-pack session (and its actions) in, what to draw out; null for any other game. */
export function chanceView(session: Session, actions?: SessionAction[] | null, now: number = Date.now()): ChanceView | null {
  const d = stateData(session) as Data;
  const hands = obj<{ values?: unknown }>(obj<unknown>(d.deal).hands);
  const reveals = Object.keys(obj<unknown>(obj<unknown>(d.rand).reveals));
  const result = verdict(actions);
  if (isLiarsDice(session.module_type)) {
    const bids = Array.isArray(d.bids) ? (d.bids as Bid[]) : [];
    const order = Array.isArray(d.turn_order) ? (d.turn_order as string[]) : [];
    const ended = (d.ended ?? null) as { by: string; bid: number; bidder: string } | null;
    return {
      kind: "dice",
      players: num(d.players, session.participants.length),
      diceEach: num(d.dice, 5),
      onesWild: d.ones_wild === true,
      maxBids: num(d.max_bids, 30),
      order,
      bids,
      call: ended ? { by: ended.by, bid: ended.bid, bidder: ended.bidder } : null,
      revealed: reveals,
      dice: Object.fromEntries(Object.entries(hands).map(([p, h]) => [p, Array.isArray(h?.values) ? (h.values as number[]) : []])),
      toMove: session.status === "active" && !ended && order.length ? order[bids.length % order.length] : null,
      result,
      secondsLeft: ended ? null : secondsLeft(session, d, "turn_started_at", now),
    };
  }
  if (isCvAuction(session.module_type)) {
    const phase = d.phase === "reveal" || d.phase === "secrets" ? d.phase : "commit";
    return {
      kind: "auction",
      players: num(d.players, session.participants.length),
      spread: num(d.spread, 10),
      phase,
      committed: Object.keys(obj<unknown>(d.commitments)),
      bids: obj<number>(d.revealed),
      secrets: reveals,
      base: typeof d.base_value === "number" ? d.base_value : null,
      signals: Object.fromEntries(Object.entries(hands).filter(([, h]) => typeof h?.values === "number").map(([p, h]) => [p, h.values as number])),
      result,
      secondsLeft: phase === "secrets" ? null : secondsLeft(session, d, "phase_started_at", now),
    };
  }
  return null;
}

/** One short line for a list row. */
export function chanceLine(v: ChanceView, session: Pick<Session, "status" | "participants">): string {
  if (session.status === "waiting") return `waiting for players (${session.participants.length}/${v.players})`;
  if (v.result) return v.result.winner ? `${v.result.winner} won` : (v.result.termination ?? "no result").replace(/_/g, " ");
  if (v.kind === "dice") return v.call ? "called: revealing" : `${v.bids.length} bid${v.bids.length === 1 ? "" : "s"}`;
  return v.phase === "commit" ? `${v.committed.length}/${v.players} sealed` : v.phase === "reveal" ? `${Object.keys(v.bids).length}/${v.players} revealed` : "revealing secrets";
}
