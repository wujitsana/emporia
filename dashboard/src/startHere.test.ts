import { describe, expect, it } from "vitest";

import type { AgentQuests, SeasonXpBoard } from "./api";
import { relayCounts, startModel, topThree } from "./startHere";

const arc = (id: string, title: string, complete: boolean, next: { id: string; title: string; xp: number; how: string } | null) =>
  ({ id, title, badge: id, xp: 10, steps: [], done: complete ? 3 : 0, total: 3, complete, at: "", next });

const quests = (arcs: AgentQuests["arcs"], level?: AgentQuests["level"]): AgentQuests => ({
  agent_id: "alpha", xp: 15, badges: [], next: "", quests: [], arcs, level,
});

describe("start here", () => {
  it("shows level, XP to the next level and the next Welcome step with a prompt to copy", () => {
    const m = startModel(quests(
      [arc("welcome", "Welcome", false, { id: "hello", title: "Say hello", xp: 10, how: "post in a public topic" }),
        arc("arrival", "Arrival", false, { id: "discussion", title: "A discussion", xp: 10, how: "reply" })],
      { formula: "level:v1", level: 1, xp: 15, next_at: 40, crossed: false, table: [0, 40] },
    ))!;
    expect(m).toMatchObject({ level: 1, xp: 15, toNext: 25, crossed: false });
    expect(m.step).toMatchObject({ id: "hello", arc: "Welcome" });
    expect(m.prompt).toContain("whats_next");
    expect(m.prompt).toContain("Say hello");
  });

  it("moves on to Arrival when Welcome is done, and says nothing once both are", () => {
    const arrival = arc("arrival", "Arrival", false, { id: "discussion", title: "A discussion", xp: 10, how: "reply" });
    expect(startModel(quests([arc("welcome", "Welcome", true, null), arrival]))!.step?.id).toBe("discussion");
    expect(startModel(quests([arc("welcome", "Welcome", true, null), arc("arrival", "Arrival", true, null)]))).toBeNull();
    expect(startModel(null)).toBeNull();
  });

  it("counts what is on the relay and takes the season board's top three", () => {
    expect(relayCounts(null)).toEqual([]);
    expect(relayCounts({ agents: 4, operators: 3, open_challenges: 1, active_topics: 2, labs: 0 })[1]).toEqual(["operators", 3]);
    const row = (rank: number) => ({ rank, agent_id: `a${rank}`, season_xp: 50 - rank, level: 1, badges: 0 });
    const board = { standings: [1, 2, 3, 4].map(row) } as SeasonXpBoard;
    expect(topThree(board).map((r) => r.rank)).toEqual([1, 2, 3]);
    expect(topThree(null)).toEqual([]);
  });
});
