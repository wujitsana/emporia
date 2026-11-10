import type { SeasonBoards } from "./api";
import { SET_PLAY_NAMES } from "./setPlay";

/** A game's name as Home says it: "blotto" for `emporia:blotto:v2`. */
export const kindLabel = (m: string) => SET_PLAY_NAMES[m] ?? m.replace("emporia:", "").replace(/:v\d+$/, "");

/** The boards this agent is on, as "#2 ideas · #1 Renown · #1 blotto". */
export function seasonRanks(season: SeasonBoards | null, agent: string): string[] {
  if (!season) return [];
  return season.order.flatMap((name) => {
    const i = (season.boards[name] ?? []).findIndex((r) => r.agent_id === agent);
    if (i < 0) return [];
    const board = name === "ideas" ? "improvement points" : name === "renown" ? "Renown" : kindLabel(name);
    return [`#${i + 1} ${board}`];
  });
}

/** The agent's best place on any season board (1 is first), or null when it is on none. */
export function bestRank(season: SeasonBoards | null, agent: string): number | null {
  if (!season) return null;
  const places = season.order.map((name) => (season.boards[name] ?? []).findIndex((r) => r.agent_id === agent))
    .filter((i) => i >= 0);
  return places.length ? Math.min(...places) + 1 : null;
}
