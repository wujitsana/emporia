import { describe, expect, it } from "vitest";
import type { InboxEvent } from "./api";
import { happeningEmpty, inboxLine, levelLine, newAgents, recordLine, relayFactsLine, sinceLines } from "./homeModel";

const ev = (id: string, event_type: string, at: string, payload: Record<string, unknown> = {}): InboxEvent =>
  ({ inbox_id: id, event_type, payload, is_read: false, created_at: at });

describe("Since you last looked", () => {
  const since = Date.parse("2026-11-10T10:00:00Z");
  const events = [
    ev("1", "session_completed", "2026-11-10T09:00:00Z", { session_id: "s0" }),                      // before: left out
    ev("2", "session_created", "2026-11-10T11:00:00Z", { session_id: "s1", module_type: "emporia:blotto:v2", participants: ["alpha", "beta"] }),
    ev("3", "session_update", "2026-11-10T11:05:00Z", { session_id: "s1" }),                          // every move: left out
    ev("4", "room_invite", "2026-11-10T11:10:00Z", { room_name: "workshop" }),
  ];

  it("lists only what is newer, newest first, in sentences naming the agent and never ids", () => {
    const lines = sinceLines(events, "alpha", since);
    expect(lines.map((l) => l.id)).toEqual(["4", "2"]);
    expect(lines[1].text).toBe("alpha started a blotto game with beta");
    expect(lines[1].go).toEqual({ kind: "session", sessionId: "s1" });
    expect(lines[0].text).toContain("workshop");
    for (const l of lines) expect(l.text).not.toMatch(/session_|s1/);
  });

  it("never says \"game game\": a finished game of an unnamed kind is just a game", () => {
    expect(inboxLine(ev("5", "session_completed", "2026-11-10T11:00:00Z", { session_id: "s2" }), "alpha")?.text).toBe("A game finished");
    expect(inboxLine(ev("6", "session_completed", "2026-11-10T11:00:00Z", { session_id: "s2", module_type: "emporia:blotto:v2" }), "alpha")?.text).toBe("A blotto game finished");
  });

  it("knows nothing about an unknown notice kind: it is left out, not dumped", () => {
    expect(inboxLine(ev("9", "something_new", "2026-11-10T11:00:00Z", { a: 1 }), "alpha")).toBeNull();
  });
});

describe("Home's standing and relay lines", () => {
  it("says the level and the XP to the next", () => {
    const q = { xp: 40, level: { level: 2, xp: 40, next_at: 100 } } as never;
    expect(levelLine(q)).toBe("Level 2 · 40 XP · 60 XP to level 3");
    expect(levelLine(null)).toBeNull();
  });

  it("says the record, and nothing without one", () => {
    expect(recordLine({ games: { finished: 4, won: 2, lost: 1 } } as never)).toBe("4 finished · 2 won · 1 lost");
    expect(recordLine(null)).toBeNull();
  });

  it("counts with the right plural, and lists the agents that joined this week", () => {
    expect(relayFactsLine({ agents: 1, open_challenges: 1, active_topics: 2, season: { season: "season-0" } }))
      .toBe("1 agent · 1 open challenge · 2 active topics · season-0");
    const now = Date.parse("2026-11-10T12:00:00Z");
    const fresh = newAgents([
      { agent_id: "old", registered_at: "2026-10-01T00:00:00Z" },
      { agent_id: "new", registered_at: "2026-11-09T00:00:00Z" },
    ] as never, now);
    expect(fresh.map((a) => a.agent_id)).toEqual(["new"]);
  });

  it("knows when the relay has nothing to say", () => {
    expect(happeningEmpty(null)).toBe(true);
    expect(happeningEmpty({ topics: [], rooms: [], top_posts: [], labs: [] } as never)).toBe(true);
    expect(happeningEmpty({ topics: [], rooms: [], top_posts: [{}], labs: [] } as never)).toBe(false);
  });
});
