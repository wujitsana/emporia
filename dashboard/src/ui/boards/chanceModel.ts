import type { Frame } from "../../arena";
import type { BargainView } from "../../bargain";
import type { AuctionView, ChanceResult, DiceView } from "../../chance";
import type { AuctionLineProps } from "./AuctionLine";
import type { BargainSplitProps } from "./BargainSplit";
import type { DiceTableProps } from "./DiceTable";

/**
 * View models for the chance boards (liar's dice, the auction, bargain), built from a live view (the
 * Games panel) or from an Arena frame, so both draw the same thing. What the game has not revealed is
 * dropped here, not only in the component: dice, signals, V, hands and points reach a board only when
 * `over` is true, and a cup that has not been published has no values.
 */
type Name = (id: string) => string;
type FrameOf<K extends Frame["kind"]> = Extract<Frame, { kind: K }>;

const asNumber = (v: unknown): number | null => (typeof v === "number" && Number.isFinite(v) ? v : null);

function diceModel(
  o: { bids: { n: number; by: string; quantity: number; face: number }[]; call: { by: string; bid: number } | null; players: string[];
    dice: Record<string, number[]> | null; over: boolean; result: ChanceResult | null; onesWild?: boolean; diceEach?: number },
  name: Name,
): DiceTableProps {
  const ids = o.players.length ? o.players : [...new Set(o.bids.map((b) => b.by))];
  const r = o.over ? o.result : null;
  return {
    bids: o.bids.map((b) => ({ n: b.n, name: name(b.by), quantity: b.quantity, face: b.face })),
    call: o.call ? { bid: o.call.bid, byName: name(o.call.by) } : null,
    onesWild: o.onesWild ?? (r?.ones_wild === true), diceEach: o.diceEach,
    over: o.over,
    cups: ids.map((id) => ({ id, name: name(id), values: o.over && o.dice?.[id]?.length ? o.dice[id] : null })),
    showed: r ? asNumber(r.count) : null,
    held: r?.termination === "bid_held" ? true : r?.termination === "bid_failed" ? false : null,
  };
}

export function diceFromView(v: DiceView, participants: string[], name: Name = (s) => s): DiceTableProps {
  return diceModel({ bids: v.bids, call: v.call, players: participants, dice: v.dice, over: !!v.result, result: v.result,
    onesWild: v.onesWild, diceEach: v.diceEach }, name);
}

export function diceFromFrame(f: FrameOf<"dice">, name: Name): DiceTableProps {
  return diceModel({ bids: f.bids, call: f.call, players: Object.keys(f.dice ?? {}), dice: f.dice, over: !!f.result, result: f.result }, name);
}

function auctionModel(
  o: { bids: Record<string, number>; sealed: number; base: number | null; over: boolean; signals: Record<string, number> | null;
    result: ChanceResult | null; spread?: number },
  name: Name,
): AuctionLineProps {
  const r = o.over ? o.result : null;
  const value = r ? asNumber(r.value) : null;
  const bidder = r && typeof r.bidder === "string" ? r.bidder : null;
  const winBid = r ? asNumber(r.winning_bid) : null;
  const profit = r ? asNumber(r.profit) : null;
  return {
    bids: Object.entries(o.bids).map(([id, v]) => ({ id, name: name(id), value: v })),
    sealed: o.sealed,
    base: o.base,
    over: o.over,
    signals: o.over && o.signals ? Object.entries(o.signals).map(([id, v]) => ({ id, name: name(id), value: v })) : null,
    value,
    winner: bidder && winBid !== null ? { id: bidder, name: name(bidder), bid: winBid, profit } : null,
    curse: r?.termination === "winners_curse",
    spread: o.spread,
  };
}

export function auctionFromView(v: AuctionView, name: Name = (s) => s): AuctionLineProps {
  const sealed = v.committed.filter((p) => !(p in v.bids)).length;
  return auctionModel({ bids: v.bids, sealed, base: v.base, over: !!v.result, signals: v.signals, result: v.result, spread: v.spread }, name);
}

export function auctionFromFrame(f: FrameOf<"auction">, name: Name): AuctionLineProps {
  const bids = Object.fromEntries(f.slots.flatMap((s) => (s.state === "revealed" && typeof s.move === "number" ? [[s.agent, s.move]] : [])));
  return auctionModel({ bids, sealed: f.slots.filter((s) => s.state === "sealed").length, base: f.base, over: !!f.result,
    signals: f.signals, result: f.result }, name);
}

type BargainResult = NonNullable<BargainView["result"]> & { walk_aways?: Record<string, number> };

function bargainModel(
  o: { sides: string[]; pool: BargainSplitProps["pool"]; offers: { n: number; by: string; allocation: Record<string, BargainSplitProps["pool"]>; claim?: BargainSplitProps["pool"] }[];
    ended: { how: "accepted" | "declined" } | null; hands: BargainView["hands"] | null; result: BargainView["result"]; over: boolean },
  name: Name,
): BargainSplitProps {
  const r = (o.over ? o.result : null) as BargainResult | null;
  const accepted = o.ended?.how === "accepted" && o.offers.length ? o.offers[o.offers.length - 1].n : null;
  const hands = o.over && o.hands && Object.keys(o.hands).length ? o.hands : null;
  return {
    sides: o.sides.map((id) => ({ id, name: name(id) })),
    pool: o.pool,
    offers: o.offers.map((x) => ({ n: x.n, byName: name(x.by), give: x.allocation, claim: x.claim })),
    accepted, over: o.over, hands,
    points: r?.points ?? null, walkAways: r?.walk_aways ?? null, claims: r?.claims ?? null,
    how: o.ended?.how ?? null,
  };
}

export function bargainFromView(v: BargainView, participants: string[], over: boolean, name: Name = (s) => s): BargainSplitProps {
  return bargainModel({ sides: participants.slice(0, 2), pool: v.pool, offers: v.offers, ended: v.ended, hands: v.hands, result: v.result, over }, name);
}

export function bargainFromFrame(f: FrameOf<"bargain">, name: Name): BargainSplitProps {
  return bargainModel({ sides: f.players ?? [], pool: f.pool ?? {}, offers: f.offers, ended: f.ended, hands: f.hands, result: f.result, over: !!f.result }, name);
}
