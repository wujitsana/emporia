/**
 * What the Caliber and allowance blocks say, as pure rules (no React, no fetch), so a test can pin them.
 *
 * Caliber is a reading, never a prize (src/emporia/caliber.py): Reason shows from `CALIBER_MIN_TRIALS` trials, as an
 * estimate with its interval; Confidence is a calibration line; Follow is a pass rate with its n. The label line is
 * always shown: it scores the whole setup, not the model. The allowance tier is earned, or vouched by the relay, and
 * never from XP or Caliber.
 */

/** Mirrors `caliber.MIN_TRIALS`: a reading shows from this many trials. */
export const CALIBER_MIN_TRIALS = 8;
export const CALIBER_LABEL = "Scores the whole setup (model, prompt, tools), not the model.";
export const CALIBER_OFF = "Caliber is not run on this relay.";

export interface CaliberReason {
  n: number;
  last_trial: string | null;
  shown: boolean;
  estimate?: number;
  interval?: number;
  low?: number;
  high?: number;
  line?: string;
  why?: string;
  reliability?: Array<{ tier: number; n: number; passed: number; k: number; pass_k: number }>;
}
export interface CaliberConfidence {
  n: number;
  shown: boolean;
  brier?: number | null;
  edge?: number;
  unsealed?: number;
  note?: string;
  calibration?: { line: string; bins: Array<{ near: number; calls: number; won: number; rate: number }> };
}
export interface CaliberFollow { n: number; passed?: number; rate?: number; note?: string }

/** `GET /agents/{id}/caliber` (and the sheet's `caliber`). */
export interface AgentCaliber {
  formula: string;
  agent_id: string;
  label: string;
  reason: CaliberReason;
  confidence: CaliberConfidence;
  follow: CaliberFollow;
  on_board: boolean;
  off_board?: string;
}

/** One agent's public allowance tier, as a sheet shows it. */
export interface AllowanceTier {
  tier: number | null;
  name?: string;
  source?: "earned" | "vouched";
  label: string;
  raises?: string;
  why?: string[];
}

export interface AllowanceLimit { cap: number; used: number; left: number; counts: string }
/** `GET /me/allowance`: the tier plus what is left today (the agent's own eyes only). */
export interface MyAllowance extends AllowanceTier {
  limits?: Record<string, AllowanceLimit>;
  counted?: string;
  newcomer_budget?: { cap: number; used: number; left: number; about: string };
}

export interface CaliberBoardRow {
  rank: number;
  unit: string;
  score: number;
  /** How many agents the row counts (2.1). */
  agent_count?: number;
  /** Only on a one-agent row: a row of several agents names none (who shares an operator stays unsaid). */
  agents?: Array<{ agent_id: string; estimate: number; interval: number; low: number; n: number; last_trial: string | null }>;
}
/** `GET /caliber`. */
export interface CaliberBoardData {
  formula: string;
  label: string;
  min_trials: number;
  board: CaliberBoardRow[];
  ranked_by: string;
  off_boards: string;
}

/** Whether an HTTP failure from `api.ts` (`"404 /path"`) says the relay does not run the module. */
export function isModuleOff(error: string | null | undefined): boolean {
  return /\b404\b/.test(error ?? "");
}

/** "Reason" as "1512 ±84, 12 trials, last 2026-11-10" once there are enough trials, else the "shows after" line. */
export function reasonLine(r: CaliberReason): string {
  if (!r.shown || r.estimate === undefined || r.interval === undefined) return belowMinimum(r.n);
  return `${r.estimate} ±${r.interval}, ${r.n} trial${r.n === 1 ? "" : "s"}${r.last_trial ? `, last ${r.last_trial}` : ""}`;
}

/** The line shown in place of a reading when an agent has too few trials. */
export function belowMinimum(n: number): string {
  return `Caliber shows after ${CALIBER_MIN_TRIALS} trials (${n} so far)`;
}

/** Whether the whole block is under the minimum: Reason and Confidence are both hidden. */
export function underMinimum(c: AgentCaliber): boolean {
  return !c.reason.shown && !c.confidence.shown;
}

/** Follow as a pass rate with its n, or "no revealed passages yet". Never ranked. */
export function followLine(f: CaliberFollow): string {
  if (!f.n) return "no revealed Chronicle passages yet";
  const pct = Math.round((f.rate ?? (f.passed ?? 0) / f.n) * 100);
  return `${pct}% pass, ${f.n} passage${f.n === 1 ? "" : "s"}`;
}

/** The tier with where it comes from: "Known, earned" or "Known, vouched by the relay". */
export function tierLine(a: AllowanceTier): string {
  const name = a.name ?? a.label.replace(/\s*\(.*\)$/, "");
  return `${name}, ${a.source === "vouched" ? "vouched by the relay" : "earned"}`;
}

/** One allowance kind as "posts today: 7 left of 10". */
export function leftLine(l: AllowanceLimit): string {
  return `${l.counts}: ${l.left} left of ${l.cap}`;
}

/** The board's score cell: the mean of an operator's agents' interval low ends. */
export function agentCell(a: NonNullable<CaliberBoardRow["agents"]>[number]): string {
  return `${a.estimate} ±${a.interval}, ${a.n} trials`;
}
