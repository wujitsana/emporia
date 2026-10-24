import type { GameResult } from "./api";
import { shortAgent } from "./chessReplay";

/** P134: how a finished game ended, in a few words. */
export function verdict(r: Pick<GameResult, "winner" | "outcome_type" | "termination">): string {
  if (r.outcome_type === "draw") return "draw — stakes refunded";
  if (!r.winner) return r.outcome_type ?? "ended";
  const how = r.termination === "timeout" ? " on time"
    : r.termination?.includes("CHECKMATE") ? " by checkmate" : "";
  return `${shortAgent(r.winner, 16)} won${how}`;
}
