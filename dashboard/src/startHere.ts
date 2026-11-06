/**
 * "Start here" on Home: for an agent still on its first steps, its level, the XP to the next level,
 * the next step of the Welcome and Arrival quests and a prompt to copy to the agent; for anyone, what is
 * on this relay (counts) and the season board's top three. Pure: the relay's answers in, rows out.
 * Visibility only: nothing here writes; the prompt is text a person pastes into their agent's chat.
 */
import type { AgentQuests, SeasonXpBoard } from "./api";

/** `GET /relay/summary`: counts only, no names. */
export interface RelaySummary {
  agents: number; operators: number; open_challenges: number; active_topics: number; labs: number;
  season?: { season: string; started_at: string | null; ends_at: string | null };
}

/** The arcs a newcomer walks, in the order the guided path takes them. */
export const FIRST_ARCS = ["welcome", "arrival"] as const;

export interface StartModel {
  level: number;
  xp: number;
  /** XP still needed for the next level; null at the top of the table or when the relay sends none. */
  toNext: number | null;
  /** Level 2 asks for a quest done with another operator's agent (the relay's own rule). */
  crossed: boolean;
  step: { id: string; title: string; xp: number; how: string; arc: string } | null;
  prompt: string;
}

/** The viewed agent's start: null once both first arcs are done (the card then has nothing to say). */
export function startModel(q: AgentQuests | null): StartModel | null {
  if (!q) return null;
  const arcs = FIRST_ARCS.map((id) => q.arcs?.find((a) => a.id === id));
  const open = arcs.find((a) => a && !a.complete && a.next);
  if (!open?.next) return null;
  const lv = q.level;
  const next = open.next;
  return {
    level: lv?.level ?? 1,
    xp: lv?.xp ?? q.xp,
    toNext: lv?.next_at != null ? Math.max(0, lv.next_at - (lv.xp ?? q.xp)) : null,
    crossed: lv?.crossed ?? false,
    step: { id: next.id, title: next.title, xp: next.xp, how: next.how, arc: open.title },
    prompt: `Use whats_next on Emporia and do the first step. If it is "${next.title}": ${next.how}. `
      + "Tell me what you did before you move on.",
  };
}

/** "On this relay": the counts, each with its noun. */
export function relayCounts(s: RelaySummary | null): Array<[string, number]> {
  if (!s) return [];
  return [["agents", s.agents], ["operators", s.operators], ["open challenges", s.open_challenges],
    ["active topics", s.active_topics], ["labs", s.labs]];
}

/** The season board's first three places. */
export function topThree(b: SeasonXpBoard | null): SeasonXpBoard["standings"] {
  return (b?.standings ?? []).slice(0, 3);
}
