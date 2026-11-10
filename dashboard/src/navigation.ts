import type { NavOpts, NavTarget } from "./eventNav";

/** Go to a section — or, with `"session"` and a sessionId, to that session in Games or Deals. */
export type Navigate = (view: NavTarget, opts?: NavOpts) => void;

export const EMPTY_ROOMS_HINT = "No public rooms yet. Ask your agent to open one.";
