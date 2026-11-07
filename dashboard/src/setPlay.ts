/**
 * The set plays (blotto, guess ⅔ of the average, lowest unique) as a viewer
 * sees them: which phase the table is in, what each seat has done, and — once
 * everything is revealed — who won and what won it.
 *
 * Pure: a session's state in, a view model out. It mirrors the relay's rules
 * (src/emporia/modules/blotto.py, two_thirds.py, lowest_unique.py) only to
 * *show* an outcome; the relay's verdict is the one that settles anything.
 * When the final action carries the relay's verdict, that verdict wins.
 */
import type { Session, SessionAction } from "./api";

// Calls moved the set plays to `:v2` (src/emporia/calls.py); the `:v1` ones are retired —
// none is created any more, but their games stay in the history and draw the same way.
export const BLOTTO = "emporia:blotto:v2";
export const TWO_THIRDS = "emporia:two-thirds:v2";
export const LOWEST_UNIQUE = "emporia:lowest-unique:v2";
export const SET_PLAYS = [BLOTTO, TWO_THIRDS, LOWEST_UNIQUE] as const;
/** Bargain (gamify-bargain): a set play won by talking — turns of offers, no commit and reveal. */
export const BARGAIN = "emporia:bargain:v2";

/** A module type without its version: `emporia:blotto:v2` → `emporia:blotto`. */
export function gameBase(moduleType: string | undefined | null): string {
  return (moduleType ?? "").replace(/:v\d+$/, "");
}

const COMMIT_REVEAL = new Set<string>(SET_PLAYS.map(gameBase));

/** The commit-and-reveal set plays (blotto, ⅔, lowest unique), any version. */
export function isSetPlay(moduleType: string | undefined | null): boolean {
  return !!moduleType && COMMIT_REVEAL.has(gameBase(moduleType));
}

export function isBargain(moduleType: string | undefined | null): boolean {
  return !!moduleType && gameBase(moduleType) === gameBase(BARGAIN);
}

/** The games pack (src/emporia/modules/liars_dice.py, cv_auction.py): drawn from the randomness service. */
const CHANCE = new Set(["emporia:liars-dice", "emporia:cv-auction"]);
export function isChanceModule(moduleType: string | undefined | null): boolean {
  return !!moduleType && CHANCE.has(gameBase(moduleType));
}

/** What the Games section lists under "set plays": the commit-and-reveal ones, Bargain and the games pack. */
export function isTablePlay(moduleType: string | undefined | null): boolean {
  return isSetPlay(moduleType) || isBargain(moduleType) || isChanceModule(moduleType);
}

const NAMES: Record<string, string> = {
  "emporia:blotto": "blotto",
  "emporia:two-thirds": "guess ⅔ of the average",
  "emporia:lowest-unique": "lowest unique",
  "emporia:bargain": "bargain",
  "emporia:liars-dice": "liar's dice",
  "emporia:cv-auction": "common-value auction",
};
/** A set play's name, keyed by every version the relay has served. */
export const SET_PLAY_NAMES: Record<string, string> = Object.fromEntries(
  Object.entries(NAMES).flatMap(([base, name]) => [[`${base}:v1`, name], [`${base}:v2`, name]]),
);

export type Phase = "waiting" | "commit" | "reveal" | "done";
export type SeatState = "to_commit" | "committed" | "revealed" | "out";

export interface Seat {
  agent: string;
  state: SeatState;
  /** The first characters of its commitment, while the move is hidden. */
  hash?: string;
  /** The revealed move: a pick, or a blotto allocation. */
  move?: number | number[];
  winner: boolean;
}

export interface Verdict {
  winner: string | null;
  draw: boolean;
  /** One plain line: "alpha took 3 of 5 fields", "closest to 20.0", … */
  why: string;
}

export interface BlottoRound {
  round: number;
  allocations: Record<string, number[]>;
  /** Per field: who took it, or null for a tie. */
  fieldWinners: (string | null)[];
  winner: string | null;
}

export interface SetPlayView {
  moduleType: string;
  /** Which board draws it, whatever the version. */
  kind: "blotto" | "two-thirds" | "lowest-unique";
  /** Calls (`:v2`): who sealed a call with its first move, and — once revealed — each call in percent. */
  called: string[];
  calls: Record<string, number>;
  phase: Phase;
  seats: Seat[];
  /** Agents the table is waiting on in this phase. */
  pending: string[];
  verdict: Verdict | null;
  /** Seconds left in this phase, or null if not known / not running. */
  secondsLeft: number | null;
  blotto?: { battlefields: number; troops: number; round: number; rounds: number; rounds_done: BlottoRound[]; wins: Record<string, number> };
  pick?: { min: number; max: number; average: number | null; target: number | null };
}

type Data = Record<string, unknown>;

/** A session's module state. The relay sends `state.data`; older fixtures sent it flat. */
export function stateData(session: Pick<Session, "state">): Data {
  const st = (session.state ?? {}) as Data;
  const d = st.data;
  return d && typeof d === "object" ? (d as Data) : st;
}

const obj = <T>(v: unknown): Record<string, T> => (v && typeof v === "object" && !Array.isArray(v) ? v as Record<string, T> : {});
const num = (v: unknown, fallback: number): number => (typeof v === "number" && Number.isFinite(v) ? v : fallback);

/** The relay's own verdict, if the action that ended the game carried one. */
export function relayVerdict(actions: SessionAction[] | null | undefined): { winner: string | null; outcome_type?: string } | null {
  for (const a of [...(actions ?? [])].reverse()) {
    const r = (a.result?.artifacts as { result?: { winner?: string | null; outcome_type?: string } } | undefined)?.result;
    if (r) return { winner: r.winner ?? null, outcome_type: r.outcome_type };
  }
  return null;
}

function fieldWinners(a: number[], b: number[], first: string, second: string): (string | null)[] {
  return a.map((x, i) => (x > (b[i] ?? 0) ? first : (b[i] ?? 0) > x ? second : null));
}

/** The same, for allocations that may not both be revealed yet: nothing to compare is no winners. */
export function fieldWinnersOf(a: number[] | undefined, b: number[] | undefined, first: string, second: string): (string | null)[] {
  return a && b ? fieldWinners(a, b, first, second) : [];
}

export function blottoRounds(data: Data, participants: string[]): BlottoRound[] {
  const [first, second] = participants;
  const history = Array.isArray(data.history) ? (data.history as Data[]) : [];
  return history.map((h) => {
    const alloc = obj<number[]>(h.allocations);
    const a = alloc[first] ?? [];
    const b = alloc[second] ?? [];
    return {
      round: num(h.round, 0),
      allocations: alloc,
      fieldWinners: fieldWinners(a, b, first, second),
      winner: (h.winner as string | null | undefined) ?? null,
    };
  });
}

/** ⅔ of the average, the closest pick, and whether the closest is shared. */
export function twoThirdsVerdict(picks: Record<string, number>): { average: number; target: number; verdict: Verdict } | null {
  const entries = Object.entries(picks);
  if (entries.length === 0) return null;
  const average = entries.reduce((n, [, g]) => n + g, 0) / entries.length;
  const target = (2 / 3) * average;
  // Compare in thirds so ties are exact, as the relay's Fraction arithmetic is.
  const sum = entries.reduce((n, [, g]) => n + g, 0);
  const dist = entries.map(([p, g]) => [p, Math.abs(3 * entries.length * g - 2 * sum)] as const);
  const best = Math.min(...dist.map(([, d]) => d));
  const closest = dist.filter(([, d]) => d === best).map(([p]) => p).sort();
  const verdict: Verdict = closest.length > 1
    ? { winner: null, draw: true, why: `${closest.join(" and ")} tie for closest to ${target.toFixed(1)} — a draw, stakes refunded` }
    : { winner: closest[0], draw: false, why: `${closest[0]} picked ${picks[closest[0]]}, closest to the target ${target.toFixed(1)}` };
  return { average, target, verdict };
}

export function lowestUniqueVerdict(picks: Record<string, number>): Verdict | null {
  const entries = Object.entries(picks);
  if (entries.length === 0) return null;
  const counts = new Map<number, number>();
  for (const [, n] of entries) counts.set(n, (counts.get(n) ?? 0) + 1);
  const unique = [...counts].filter(([, c]) => c === 1).map(([n]) => n).sort((a, b) => a - b);
  if (unique.length === 0) return { winner: null, draw: true, why: "no number was picked by only one player — a draw, stakes refunded" };
  const winner = entries.find(([, n]) => n === unique[0])![0];
  return { winner, draw: false, why: `${winner} picked ${unique[0]}, the lowest number nobody else picked` };
}

/**
 * The table as a viewer sees it. `actions` is optional: when the game is over
 * and the relay's verdict rode on the last action, it overrides the one
 * computed here.
 */
export function setPlayView(session: Session, actions?: SessionAction[] | null, now: number = Date.now()): SetPlayView {
  const data = stateData(session);
  const parts = session.participants ?? [];
  const finished = session.status === "completed";
  const waiting = session.status === "waiting";
  const commitments = obj<string>(data.commitments);
  const rawPhase = typeof data.phase === "string" ? data.phase : "commit";
  const limit = num(data.move_time_limit_s, 0);
  const startedAt = (data.phase_started_at ?? data.turn_started_at) as string | undefined;
  const secondsLeft = !finished && !waiting && limit && startedAt
    ? Math.max(0, Math.round(limit - (now - Date.parse(startedAt)) / 1000)) : null;

  const called = Array.isArray(data.called) ? (data.called as string[]) : [];
  const calls = obj<number>(data.calls);
  const base = gameBase(session.module_type);

  if (base === gameBase(BLOTTO)) {
    const rounds = blottoRounds(data, parts);
    const revealed = obj<number[]>(data.revealed);
    const wins = obj<number>(data.wins);
    const totalRounds = num(data.rounds, 1);
    let verdict: Verdict | null = null;
    if (finished) {
      const [a, b] = parts;
      const wa = wins[a] ?? 0, wb = wins[b] ?? 0;
      const relay = relayVerdict(actions);
      const winner = relay ? relay.winner : wa > wb ? a : wb > wa ? b : null;
      const last = rounds[rounds.length - 1];
      const fields = last && winner ? last.fieldWinners.filter((w) => w === winner).length : 0;
      verdict = winner
        ? { winner, draw: false, why: rounds.length
            ? (totalRounds > 1 ? `${winner} won ${Math.max(wa, wb)} of ${rounds.length} rounds` : `${winner} took ${fields} of ${last.fieldWinners.length} fields`)
            : `${winner} won — the other player ran out of time` }
        : { winner: null, draw: true, why: rounds.length ? "equal rounds — a draw, stakes refunded" : "nobody moved in time — a draw, stakes refunded" };
    }
    // The live round: who has committed, who has revealed.
    const phase: Phase = waiting ? "waiting" : finished ? "done" : rawPhase === "reveal" ? "reveal" : "commit";
    const lastRound = rounds[rounds.length - 1];
    const seats: Seat[] = parts.map((p) => {
      const winner = verdict?.winner === p;
      if (finished) return { agent: p, state: "revealed", move: lastRound?.allocations[p], winner };
      if (revealed[p]) return { agent: p, state: "revealed", move: revealed[p], winner };
      if (commitments[p]) return { agent: p, state: "committed", hash: commitments[p].slice(0, 8), winner };
      return { agent: p, state: "to_commit", winner };
    });
    const current = session.current_agent || (session.state as Data | undefined)?.current_agent as string | undefined;
    const pending = phase === "commit" ? parts.filter((p) => !commitments[p])
      : phase === "reveal" ? parts.filter((p) => !revealed[p]) : [];
    return {
      moduleType: session.module_type, kind: "blotto", called, calls, phase, seats, verdict, secondsLeft,
      pending: pending.length ? pending : current && !finished ? [current] : [],
      blotto: { battlefields: num(data.battlefields, 5), troops: num(data.troops, 100), round: num(data.round, 1),
        rounds: totalRounds, rounds_done: rounds, wins },
    };
  }

  // The pick games share one shape (PickGame in two_thirds.py).
  const picks = obj<number>(data.guesses);
  const pendingRaw = Array.isArray(data.pending) ? (data.pending as string[]) : [];
  const isTwoThirds = base === gameBase(TWO_THIRDS);
  const min = isTwoThirds ? 0 : 1;
  const max = isTwoThirds ? 100 : num(data.max_pick, 10);
  const phase: Phase = waiting ? "waiting" : finished ? "done" : rawPhase === "reveal" ? "reveal" : "commit";
  let verdict: Verdict | null = null;
  let average: number | null = null;
  let target: number | null = null;
  const allRevealed = finished || (phase === "reveal" && pendingRaw.length === 0);
  if (allRevealed) {
    if (isTwoThirds) {
      const t = twoThirdsVerdict(picks);
      if (t) ({ average, target, verdict } = t);
    } else verdict = lowestUniqueVerdict(picks);
    if (finished && !verdict) verdict = { winner: null, draw: true, why: "not everyone committed in time — a draw, stakes refunded" };
    const relay = finished ? relayVerdict(actions) : null;
    if (relay && verdict && relay.winner !== verdict.winner) {
      verdict = relay.winner ? { winner: relay.winner, draw: false, why: `${relay.winner} won (the relay's verdict)` }
        : { winner: null, draw: true, why: "a draw, stakes refunded (the relay's verdict)" };
    }
  }
  const seats: Seat[] = parts.map((p) => {
    const winner = verdict?.winner === p;
    if (picks[p] !== undefined) return { agent: p, state: "revealed", move: picks[p], winner };
    if (finished) return { agent: p, state: commitments[p] || rawPhase === "reveal" ? "out" : "to_commit", winner };
    if (commitments[p] || rawPhase === "reveal") return { agent: p, state: "committed", hash: commitments[p]?.slice(0, 8), winner };
    return { agent: p, state: "to_commit", winner };
  });
  return {
    moduleType: session.module_type, kind: isTwoThirds ? "two-thirds" : "lowest-unique", called, calls,
    phase, seats, verdict, secondsLeft,
    pending: finished ? [] : pendingRaw,
    pick: { min, max, average, target },
  };
}

/** P9: what the table's live region says — the phase and whom it waits on, never the clock,
 *  so a screen reader hears a change of turn, not the time on every poll. */
export function turnLine(v: Pick<SetPlayView, "phase" | "pending">): string {
  if (v.phase === "waiting") return "Waiting for players.";
  if (v.phase === "done") return "Finished.";
  const on = v.pending.length ? `, waiting on ${v.pending.join(", ")}` : "";
  return `${v.phase === "commit" ? "Commit" : "Reveal"} phase${on}.`;
}

/** "4 min left", "38 s left". */
export function clockText(seconds: number | null): string | null {
  if (seconds === null) return null;
  if (seconds <= 0) return "time is up";
  return seconds >= 120 ? `${Math.round(seconds / 60)} min left` : `${seconds} s left`;
}

/** One short line for a list row: where the table is. */
export function phaseLine(v: SetPlayView): string {
  if (v.phase === "waiting") return "waiting for players";
  if (v.phase === "done") return v.verdict?.winner ? `${v.verdict.winner} won` : "draw";
  const done = v.seats.filter((s) => (v.phase === "commit" ? s.state !== "to_commit" : s.state === "revealed")).length;
  return `${v.phase} · ${done} of ${v.seats.length}`;
}

/** "called 70 %" once revealed, "called · sealed" while hidden, null without a call. */
export function callText(v: SetPlayView, agent: string): string | null {
  if (typeof v.calls[agent] === "number") return `called ${v.calls[agent]} %`;
  return v.called.includes(agent) ? "called · sealed" : null;
}
