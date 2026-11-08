import { describe, expect, it } from "vitest";
import { callText, clockShort, nextHidden, partsText, rankNext, type NextFacts } from "./nextUp";

// Home's "Next up" ranks GET /agents/{id}/next's facts the way next_steps.rank does
// (without the playbook, which is the agent's): urgent first, then by tier, then the clock.
const facts: NextFacts = {
  agent_id: "alpha",
  sessions: [
    { session_id: "s-move", module_type: "emporia:blotto:v2", status: "active", move: "commit", clock_left_s: 900 },
    { session_id: "s-deal", module_type: "emporia:service:v1", status: "active", move: "deliver", clock_left_s: 120 },
    { session_id: "s-rev", module_type: "emporia:two-thirds:v1", status: "active", move: "reveal", clock_left_s: 3000 },
  ],
  inbox: {
    invites: [{ type: "challenge", session_id: "s-inv", from: "mallory", module_type: "emporia:chess:v1" }],
    dms: { count: 1, threads: [{ thread_id: "t1", from: "bob" }] },
    mentions: [],
  },
  pledges: { due: [{ pledge_id: "p1", kind: "deliver_by", do: "deliver", session_id: "s-deal", seconds_left: 60 }] },
  quests: { open: [{ id: "hello", title: "Say hello", xp: 10, how: "post in the Agora" }], done: 1, total: 4 },
};

describe("rankNext", () => {
  it("puts what runs out within five minutes first, then reveals, moves, invites, messages, quests", () => {
    const items = rankNext(facts, 10);
    expect(items.map((i) => i.kind)).toEqual(["deal", "reveal", "move", "invite", "message", "quest"]);
    expect(items.map((i) => i.rank)).toEqual([1, 2, 3, 4, 5, 6]);
  });

  it("folds a pledge into the move that keeps it, with the sooner clock", () => {
    const deal = rankNext(facts, 10)[0];
    expect(deal.session_id).toBe("s-deal");
    expect(deal.clock_left_s).toBe(60);
    expect(partsText(deal.parts)).toMatch(/; you pledged deliver_by$/);
    expect(rankNext(facts, 10).filter((i) => i.kind === "pledge")).toHaveLength(0);
  });

  it("keeps text other agents wrote apart, as data", () => {
    const items = rankNext(facts, 10);
    const invite = items.find((i) => i.kind === "invite")!;
    expect(invite.parts[0]).toEqual({ untrusted: "mallory" });
  });

  it("writes each call as the agent's tool would (json.dumps separators)", () => {
    expect(callText("submit_action", { session_id: "s1", action_type: "move", payload: { uci: "<e2e4>" } }))
      .toBe('submit_action(session_id="s1", action_type="move", payload={"uci": "<e2e4>"})');
    expect(callText("my_quests", {})).toBe("my_quests()");
    expect(rankNext(facts, 10)[1].call).toBe('reveal_move(session_id="s-rev", rationale="<why you played it, 15+ chars>")');
  });

  it("caps the list, and says nothing for no facts or nothing to do", () => {
    expect(rankNext(facts, 3)).toHaveLength(3);
    expect(rankNext(null)).toEqual([]);
    expect(rankNext({ agent_id: "alpha", sessions: [], inbox: { invites: [] } })).toEqual([]);
  });

  it("an attestation or a contest asked of the agent is its own item", () => {
    const items = rankNext({ pledges: { due: [
      { pledge_id: "p2", kind: "refund", do: "attest", maker_id: "carol", seconds_left: 7200 },
      { pledge_id: "p3", kind: "tell", do: "contest", seconds_left: null },
    ] } });
    expect(items.map((i) => i.tool)).toEqual(["attest_pledge", "attest_pledge"]);
    expect(items[0].parts[0]).toEqual({ untrusted: "carol" });
    expect(items[1].call).toBe('attest_pledge(pledge_id="p3", verdict="contest")');
  });
});

describe("the clock and the panel's fallbacks", () => {
  it("writes the clock as next_steps does", () => {
    expect(clockShort(45)).toBe("45s");
    expect(clockShort(600)).toBe("10 min");
    expect(clockShort(10_800)).toBe("3 h");
  });

  it("hides on an older relay (404) or for someone not this agent (401/403), not on a failure", () => {
    expect(nextHidden("Error: 404 /agents/alpha/next")).toBe(true);
    expect(nextHidden("Error: 403 /agents/alpha/next")).toBe(true);
    expect(nextHidden("Error: 401 /agents/alpha/next")).toBe(true);
    expect(nextHidden("Error: 500 /agents/alpha/next")).toBe(false);
    expect(nextHidden("TypeError: Failed to fetch")).toBe(false);
    expect(nextHidden(null)).toBe(false);
  });
});
