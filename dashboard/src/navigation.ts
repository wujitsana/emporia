import type { NavOpts, NavTarget } from "./eventNav";

/** Go to a section — or, with `"session"` and a sessionId, to that session in Games or Deals. */
export type Navigate = (view: NavTarget, opts?: NavOpts) => void;

export const EMPTY_SEED_HINT =
  "No data yet. Seed the relay: emporia/scripts/seed_demo_relay.py (EMPORIA_GAMES_DB must match relay HOME).";
