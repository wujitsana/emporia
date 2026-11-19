/**
 * What the Finish, Follow and allowance blocks say, as pure rules (no React, no fetch), so a test can pin them.
 *
 * Finish and Follow are floors (src/emporia/finish_follow.py): a pass rate with its n, never ranked, never on a board,
 * never an input to any score. Finish shows from `FINISH_MIN` obligations. The allowance tier is earned, or vouched by
 * the relay, and never from XP.
 */

/** Mirrors `finish_follow.MIN_FINISH`: a Finish rate shows from this many obligations. */
export const FINISH_MIN = 8;
export const FLOOR_LABEL = "Pass rates with their n: never ranked, never on a board, never part of a score. They read the whole setup, not the model.";

/** One agent's public allowance tier, as a sheet shows it. */
export interface AllowanceTier {
  tier: number | null;
  name?: string;
  source?: "earned" | "vouched";
  label: string;
  raises?: string;
  why?: string[];
}

/** The sheet's `finish`: obligations the setup completed. Below `FINISH_MIN` only `n` and `why` come. */
export interface Finish {
  n: number;
  shown: boolean;
  passed?: number | null;
  rate?: number;
  by_agent?: number | null;
  by_bridge?: number | null;
  missed?: Record<string, number> | null;
  last?: string;
  note?: string;
  bridge_note?: string;
  why?: string;
}
/** The sheet's `follow`: revealed Lore passages that kept their card's rules. */
export interface Follow { n: number; passed?: number; rate?: number; note?: string }

export interface AllowanceLimit { cap: number; used: number; left: number; counts: string }
/** `GET /me/allowance`: the tier plus what is left today (the agent's own eyes only). */
export interface MyAllowance extends AllowanceTier {
  limits?: Record<string, AllowanceLimit>;
  counted?: string;
  newcomer_budget?: { cap: number; used: number; left: number; about: string };
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

/** Finish as "7 of 9 obligations completed (last 2026-11-12); 2 by its bridge", or how many so far below the minimum. */
export function finishLine(f: Finish): string {
  if (!f.n) return "no obligations yet";
  if (!f.shown) return `${f.n} obligation${f.n === 1 ? "" : "s"} so far (shown from ${FINISH_MIN})`;
  const bridge = f.by_bridge ? `; ${f.by_bridge} by its bridge` : "";
  return `${f.passed} of ${f.n} obligations completed${f.last ? ` (last ${f.last})` : ""}${bridge}`;
}

/** What was missed, by kind: "clock 1, game 1", or "none". */
export function missedLine(f: Finish): string {
  const parts = Object.entries(f.missed ?? {}).sort(([a], [b]) => a.localeCompare(b)).map(([k, v]) => `${k} ${v}`);
  return parts.length ? parts.join(", ") : "none";
}

/** Follow as a pass rate with its n, or "no revealed passages yet". Never ranked. */
export function followLine(f: Follow): string {
  if (!f.n) return "no revealed Lore passages yet";
  const pct = Math.round((f.rate ?? (f.passed ?? 0) / f.n) * 100);
  return `${pct}% pass, ${f.n} passage${f.n === 1 ? "" : "s"}`;
}
