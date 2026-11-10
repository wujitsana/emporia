/**
 * The Arena's step-through replay, as a pure model: a session and its action log in, the state of the
 * match at step i out. Step 0 is the table before anyone moved; step i is the table after the i-th
 * action. Nothing here talks to the relay and nothing writes.
 *
 * Hidden things stay hidden until the record reveals them:
 * - a commit shows as "sealed" until its own reveal step (the hash is never drawn as a move);
 * - the secrets and nonces of the randomness games are never drawn at all;
 * - dice, signals, the value V and the hands appear only on the last step of a finished game, because
 *   that is when the relay publishes them (`state.data.deal`).
 */
import type { Session, SessionAction } from "./api";
import { bareAgentName } from "./agentName";
import { bargainView, type BargainOffer, type BargainView, type Goods } from "./bargain";
import { chanceView, isCvAuction, isLiarsDice, type Bid, type ChanceResult } from "./chance";
import { isBargain, isSetPlay, gameBase, setPlayView, stateData, type Verdict, fieldWinnersOf } from "./setPlay";
import { SET_PLAYS } from "./setPlay";

export type ArenaKind = "blotto" | "two-thirds" | "lowest-unique" | "dice" | "auction" | "bargain" | "chess" | "log";

export interface Step {
  /** 0 is the start; i is after the i-th action. */
  index: number;
  actionId: string | null;
  agent: string | null;
  type: string;
  /** One plain line: what happened at this step. Never a hidden move. */
  label: string;
}

export interface Slot {
  agent: string;
  state: "waiting" | "sealed" | "revealed";
  /** The revealed pick or allocation. */
  move?: number | number[];
  /** "called 70 %" once revealed, "called" while sealed. */
  call?: string;
}

export interface RoundRecord { round: number; moves: Record<string, number | number[]>; fieldWinners: (string | null)[] }

export type Frame =
  | {
      kind: "blotto" | "two-thirds" | "lowest-unique";
      round: number;
      rounds: number;
      phase: "commit" | "reveal";
      slots: Slot[];
      past: RoundRecord[];
      /** Blotto: this round's field winners, once both allocations are revealed. */
      fields: (string | null)[] | null;
      verdict: Verdict | null;
    }
  | {
      kind: "dice";
      bids: Bid[];
      call: { by: string; bidder: string; bid: number } | null;
      revealed: string[];
      /** Published with the record; null before the last step. */
      dice: Record<string, number[]> | null;
      result: ChanceResult | null;
    }
  | {
      kind: "auction";
      slots: Slot[];
      secrets: string[];
      /** V, the signals and the base value: the last step of a finished game only. */
      value: number | null;
      base: number | null;
      signals: Record<string, number> | null;
      result: ChanceResult | null;
    }
  | {
      kind: "bargain";
      offers: BargainOffer[];
      ended: { how: "accepted" | "declined"; by: string } | null;
      hands: Record<string, Goods> | null;
      result: BargainView["result"];
    }
  | { kind: "chess" }
  | { kind: "log"; rows: { n: number; agent: string; type: string; text: string }[] };

export interface Timeline {
  kind: ArenaKind;
  players: string[];
  /** Position 0..steps.length-1 (so `steps.length - 1` actions). */
  steps: Step[];
  finished: boolean;
  frameAt(i: number): Frame;
}

type Payload = Record<string, unknown>;
const HIDDEN_KEYS = new Set(["s", "nonce", "commitment", "sig", "signature", "secret"]);

/** Only the actions that took effect: a rejected move is not a step of the match. */
export function effectiveActions(actions: SessionAction[] | null | undefined): SessionAction[] {
  return (actions ?? []).filter((a) => a.result?.success !== false);
}

export function arenaKind(moduleType: string): ArenaKind {
  const base = gameBase(moduleType);
  if (base === "emporia:chess") return "chess";
  if (isSetPlay(moduleType)) {
    return base === gameBase(SET_PLAYS[0]) ? "blotto" : base === gameBase(SET_PLAYS[1]) ? "two-thirds" : "lowest-unique";
  }
  if (isBargain(moduleType)) return "bargain";
  if (isLiarsDice(moduleType)) return "dice";
  if (isCvAuction(moduleType)) return "auction";
  return "log";
}

const isSecretReveal = (a: SessionAction) => a.action_type === "reveal" && a.payload && "s" in a.payload;
const goodsText = (g: Goods | undefined) => Object.entries(g ?? {}).filter(([, n]) => n).map(([k, n]) => `${n} ${k}`).join(", ") || "nothing";

function movePlain(m: unknown): string {
  return Array.isArray(m) ? m.join(" / ") : String(m);
}

/** One line for what a step did. A commit never says what was committed to. */
function stepLabel(kind: ArenaKind, a: SessionAction): string {
  const p = (a.payload ?? {}) as Payload;
  switch (a.action_type) {
    case "commit": return "sealed a move";
    case "reveal":
      if (isSecretReveal(a)) return "revealed its secret";
      if (kind === "auction") return `revealed a bid of ${movePlain(p.move)}`;
      return `revealed ${movePlain(p.move)}`;
    case "bid": return `bid ${p.quantity} × ${p.face}`;
    case "liar": return "called liar";
    case "offer": return p.keep && typeof p.keep === "object" ? `offered to keep ${goodsText(p.keep as Goods)}` : "made an offer";
    case "accept": return "accepted the offer";
    case "decline": return "declined the offer";
    case "move": return `moved ${p.uci ?? p.move ?? ""}`.trim();
    case "void": return "voided the game";
    default: {
      const keys = Object.keys(p).filter((k) => !HIDDEN_KEYS.has(k));
      return keys.length ? `${a.action_type} · ${keys[0]}: ${String(p[keys[0]]).slice(0, 60)}` : a.action_type;
    }
  }
}

function setPlayFrame(kind: "blotto" | "two-thirds" | "lowest-unique", session: Session, acts: SessionAction[], i: number, players: string[], finalVerdict: Verdict | null): Frame {
  const data = stateData(session);
  const rounds = typeof data.rounds === "number" ? data.rounds : 1;
  let round = 1;
  let commits: Record<string, true> = {};
  let called: Record<string, true> = {};
  let moves: Record<string, number | number[]> = {};
  let calls: Record<string, number> = {};
  const past: RoundRecord[] = [];
  const closeRound = () => {
    const [a, b] = players;
    past.push({
      round,
      moves: { ...moves },
      fieldWinners: kind === "blotto" ? fieldWinnersOf(moves[a] as number[] | undefined, moves[b] as number[] | undefined, a, b) : [],
    });
    round += 1;
    commits = {}; called = {}; moves = {}; calls = {};
  };
  const allRevealed = () => players.length > 0 && players.every((p) => moves[p] !== undefined);
  for (const a of acts.slice(0, i)) {
    const p = (a.payload ?? {}) as Payload;
    if (a.action_type === "commit") {
      // A blotto round that is already fully revealed closes when the next round's first commit arrives.
      if (kind === "blotto" && allRevealed()) closeRound();
      commits[a.agent_id] = true;
      if (p.called) called[a.agent_id] = true;
    } else if (a.action_type === "reveal" && p.move !== undefined && !("s" in p)) {
      moves[a.agent_id] = p.move as number | number[];
      if (typeof p.call_pct === "number") calls[a.agent_id] = p.call_pct;
    }
  }
  const slots: Slot[] = players.map((agent) => {
    const call = calls[agent] !== undefined ? `called ${calls[agent]} %` : called[agent] ? "called · sealed" : undefined;
    if (moves[agent] !== undefined) return { agent, state: "revealed", move: moves[agent], call };
    if (commits[agent]) return { agent, state: "sealed", call };
    return { agent, state: "waiting", call };
  });
  const [a, b] = players;
  const fields = kind === "blotto" && allRevealed()
    ? fieldWinnersOf(moves[a] as number[] | undefined, moves[b] as number[] | undefined, a, b) : null;
  const phase = players.length > 0 && players.every((p) => commits[p]) ? "reveal" : "commit";
  return { kind, round, rounds, phase, slots, past, fields, verdict: i === acts.length ? finalVerdict : null };
}

export function buildTimeline(session: Session, actionLog: SessionAction[] | null | undefined): Timeline {
  const acts = effectiveActions(actionLog);
  const kind = arenaKind(session.module_type);
  const players = session.participants ?? [];
  const finished = session.status === "completed";
  const steps: Step[] = [
    { index: 0, actionId: null, agent: null, type: "start", label: "the table before the first move" },
    ...acts.map((a, n) => ({ index: n + 1, actionId: a.action_id, agent: a.agent_id, type: a.action_type, label: stepLabel(kind, a) })),
  ];
  const last = acts.length;
  // Published-with-the-record facts, read only for the last step of a finished game.
  const chance = chanceView(session, actionLog);
  const verdictOf = (): Verdict | null => (finished && isSetPlay(session.module_type) ? setPlayView(session, actionLog).verdict : null);

  const frameAt = (raw: number): Frame => {
    const i = Math.max(0, Math.min(last, raw));
    const atEnd = finished && i === last;
    if (kind === "blotto" || kind === "two-thirds" || kind === "lowest-unique") {
      return setPlayFrame(kind, session, acts, i, players, atEnd ? verdictOf() : null);
    }
    if (kind === "dice") {
      const bids: Bid[] = [];
      let call: { by: string; bidder: string; bid: number } | null = null;
      const revealed: string[] = [];
      for (const a of acts.slice(0, i)) {
        const p = (a.payload ?? {}) as Payload;
        if (a.action_type === "bid") bids.push({ n: bids.length + 1, by: a.agent_id, quantity: Number(p.quantity), face: Number(p.face) });
        else if (a.action_type === "liar" && bids.length) call = { by: a.agent_id, bidder: bids[bids.length - 1].by, bid: bids.length };
        else if (isSecretReveal(a) && !revealed.includes(a.agent_id)) revealed.push(a.agent_id);
      }
      const dice = atEnd && chance?.kind === "dice" && Object.values(chance.dice).some((d) => d.length) ? chance.dice : null;
      return { kind: "dice", bids, call, revealed, dice, result: atEnd && chance ? chance.result : null };
    }
    if (kind === "auction") {
      const committed = new Set<string>();
      const bids: Record<string, number> = {};
      const secrets: string[] = [];
      for (const a of acts.slice(0, i)) {
        const p = (a.payload ?? {}) as Payload;
        if (a.action_type === "commit") committed.add(a.agent_id);
        else if (isSecretReveal(a)) { if (!secrets.includes(a.agent_id)) secrets.push(a.agent_id); }
        else if (a.action_type === "reveal" && typeof p.move === "number") bids[a.agent_id] = p.move;
      }
      const slots: Slot[] = players.map((agent) =>
        bids[agent] !== undefined ? { agent, state: "revealed", move: bids[agent] }
          : committed.has(agent) ? { agent, state: "sealed" } : { agent, state: "waiting" });
      const res = atEnd && chance?.kind === "auction" ? chance : null;
      const value = res?.result && typeof (res.result as { value?: unknown }).value === "number" ? Number((res.result as { value?: unknown }).value) : null;
      return {
        kind: "auction", slots, secrets, value,
        base: res ? res.base : null,
        signals: res && Object.keys(res.signals).length ? res.signals : null,
        result: res ? res.result : null,
      };
    }
    if (kind === "bargain") {
      const bv = bargainView(session, actionLog);
      const offers: BargainOffer[] = [];
      let ended: { how: "accepted" | "declined"; by: string } | null = null;
      let n = 0;
      for (const a of acts.slice(0, i)) {
        if (a.action_type === "offer") {
          const known = bv.offers[n];
          const keep = ((a.payload ?? {}) as Payload).keep as Goods | undefined;
          offers.push(known ?? { n: n + 1, by: a.agent_id, allocation: { [a.agent_id]: keep ?? {} }, claim: ((a.payload ?? {}) as Payload).claim as Goods | undefined });
          n += 1;
        } else if (a.action_type === "accept" || a.action_type === "decline") {
          ended = { how: a.action_type === "accept" ? "accepted" : "declined", by: a.agent_id };
        }
      }
      const hands = atEnd && Object.keys(bv.hands).length ? bv.hands : null;
      return { kind: "bargain", offers, ended, hands, result: atEnd ? bv.result : null };
    }
    if (kind === "chess") return { kind: "chess" };
    return {
      kind: "log",
      rows: acts.slice(0, i).map((a, n) => ({ n: n + 1, agent: a.agent_id, type: a.action_type, text: stepLabel("log", a) })),
    };
  };
  return { kind, players, steps, finished, frameAt };
}

/** The step a link names, kept inside the log: `?step=3` on a six-step match opens step 3; a bad value is ignored. */
export function clampStep(raw: number | null | undefined, total: number): number | null {
  if (raw === null || raw === undefined || !Number.isInteger(raw) || raw < 0) return null;
  return Math.min(raw, Math.max(0, total - 1));
}

/** Declared-model standing for one player, in words (declared, never verified). */
export function modelStanding(p: { status: string; model: string | null } | undefined, ended: boolean): string {
  if (!p) return "no model declared";
  if (p.status === "revealed" && p.model) return `${p.model} · revealed`;
  if (p.status === "mismatch") return "model mismatch";
  if (p.status === "committed") return ended ? "model missing" : "model declared · sealed";
  if (p.status === "none" || p.status === "missing") return ended ? "model missing" : "no model declared";
  return p.status;
}

/** "alpha@host" when the relay knows the address, else a short id. */
export function playerName(id: string, book: Record<string, string> | undefined, max = 18): string {
  const addr = book?.[id];
  if (addr) return bareAgentName(addr);
  if (id.includes("@")) return bareAgentName(id);
  return id.length <= max ? id : `…${id.slice(-max + 1)}`;
}
