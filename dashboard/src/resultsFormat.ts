import type { GameResult } from "./api";
import { shortAgent } from "./chessReplay";

/** P134: how a finished game ended, in a few words. */
export function verdict(r: Pick<GameResult, "winner" | "outcome_type" | "termination"> & { points?: Record<string, number>; participants?: string[] }): string {
  // A Bargain deal both sides accepted: nobody "lost"; each side's own points say who came out ahead.
  if (r.termination === "deal" && r.points && Object.keys(r.points).length > 1) {
    const ids = r.participants?.length ? r.participants : Object.keys(r.points);
    return `deal: ${ids.map((p) => r.points![p] ?? 0).join(" vs ")} points`;
  }
  if (r.outcome_type === "draw") return "draw";
  if (!r.winner) return r.outcome_type ?? "ended";
  const how = r.termination === "timeout" ? " on time"
    : r.termination?.includes("CHECKMATE") ? " by checkmate" : "";
  return `${shortAgent(r.winner, 16)} won${how}`;
}
