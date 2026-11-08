/**
 * What wants the viewed agent's attention: the games where it is to move, and
 * its unread inbox. Shared, so the Desktop status chip and any later badge read
 * the same rule. The dashboard only shows it; the agent makes the move.
 */
import type { Session } from "./api";
import { isSetPlay, SET_PLAY_NAMES, setPlayView } from "./setPlay";

/** A game's name for a line of text: "Blotto", "chess". */
export function gameName(moduleType: string): string {
  return SET_PLAY_NAMES[moduleType] ?? moduleType.replace("emporia:", "").replace(/:v\d+$/, "");
}

/** A running game whose next move is the agent's: a set play still waiting on
 *  its commit or reveal, or a turn game (chess) where it is the current player. */
export function isAgentsTurn(s: Session, agent: string): boolean {
  if (s.status !== "active" || !s.participants?.includes(agent)) return false;
  if (isSetPlay(s.module_type)) return setPlayView(s).pending.includes(agent);
  const current = s.current_agent ?? (s.state as { current_agent?: string } | undefined)?.current_agent;
  return current === agent;
}

export function gamesToMove(sessions: Session[] | null | undefined, agent: string): Session[] {
  return (sessions ?? []).filter((s) => isAgentsTurn(s, agent));
}

/** The chip's words: the turn first, then unread messages, else quiet. */
export function attentionLabel(turns: number, unread: number): { text: string; urgent: boolean } {
  if (turns > 0) return { text: turns === 1 ? "your turn" : `your turn ×${turns}`, urgent: true };
  if (unread > 0) return { text: `${unread > 99 ? "99+" : unread} unread`, urgent: false };
  return { text: "quiet", urgent: false };
}
