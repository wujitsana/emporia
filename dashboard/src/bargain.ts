/**
 * Bargain (`emporia:bargain:v2`) as a viewer sees it — pure: a session (and its
 * actions) in, what to draw out. Drawn by ui/BargainPanel.tsx. While the game runs
 * the hands are sealed; a viewer who is a player gets its own values (`private`).
 */
import type { Session, SessionAction } from "./api";
import { stateData } from "./setPlay";

export const GOODS = ["silk", "spice", "amber"] as const;
export type Goods = Partial<Record<(typeof GOODS)[number], number>>;

export interface BargainOffer {
  n: number;
  by: string;
  allocation: Record<string, Goods>;
  claim?: Goods;
}

export interface BargainView {
  pool: Goods;
  total: number;
  maxOffers: number;
  walkAway: number;
  offers: BargainOffer[];
  ended: { how: "accepted" | "declined"; by: string; offer: number } | null;
  /** The hands, once the game is over and the relay has published them. */
  hands: Record<string, Goods>;
  /** The relay's verdict, from the action that ended the game. */
  result: {
    winner: string | null; termination?: string; reason?: string;
    points?: Record<string, number>;
    claims?: Record<string, { claimed: Goods; actual: Goods; matches: boolean }>;
  } | null;
  secondsLeft: number | null;
  /** The viewer's own values, while it plays (`private`); never another's. */
  mine: Goods | null;
}

type Data = Record<string, unknown>;

/** Pure: a bargain's session (and its actions) in, what to draw out. */
export function bargainView(session: Session, actions?: SessionAction[] | null, now: number = Date.now()): BargainView {
  const d = stateData(session) as Data;
  const deal = (d.deal ?? {}) as { hands?: Record<string, { values?: Goods }> | null };
  const hands = Object.fromEntries(Object.entries(deal.hands ?? {}).map(([p, h]) => [p, h?.values ?? {}]));
  let result: BargainView["result"] = null;
  for (const a of [...(actions ?? [])].reverse()) {
    const r = (a.result?.artifacts as { result?: BargainView["result"] } | undefined)?.result;
    if (r) { result = r; break; }
  }
  const limit = typeof d.move_time_limit_s === "number" ? d.move_time_limit_s : 0;
  const started = typeof d.turn_started_at === "string" ? Date.parse(d.turn_started_at) : NaN;
  const running = session.status === "active";
  const priv = (session as Session & { private?: { values?: Goods } | null }).private;
  return {
    pool: (d.pool ?? {}) as Goods,
    total: typeof d.total === "number" ? d.total : 10,
    maxOffers: typeof d.max_offers === "number" ? d.max_offers : 6,
    walkAway: typeof d.walk_away === "number" ? d.walk_away : 0,
    offers: Array.isArray(d.offers) ? (d.offers as BargainOffer[]) : [],
    ended: (d.ended ?? null) as BargainView["ended"],
    hands,
    result,
    secondsLeft: running && limit && Number.isFinite(started) ? Math.max(0, Math.round(limit - (now - started) / 1000)) : null,
    mine: priv?.values ?? null,
  };
}
