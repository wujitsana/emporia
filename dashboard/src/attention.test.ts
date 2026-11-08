import { describe, expect, it } from "vitest";
import type { Session } from "./api";
import { attentionLabel, gamesToMove, isAgentsTurn } from "./attention";
import { BLOTTO } from "./setPlay";

const session = (over: Partial<Session>): Session => ({
  session_id: "s1", module_type: BLOTTO, status: "active", participants: ["alpha", "beta"],
  current_agent: "", created_at: "2026-11-05T00:00:00Z", ...over,
} as Session);

describe("whose move it is", () => {
  it("a set play waits on whoever has not committed, then on whoever has not revealed", () => {
    const commit = session({ state: { phase: "commit", commitments: { beta: "abc" } } });
    expect(isAgentsTurn(commit, "alpha")).toBe(true);
    expect(isAgentsTurn(commit, "beta")).toBe(false);
    const reveal = session({ state: { phase: "reveal", commitments: { alpha: "a", beta: "b" }, revealed: { alpha: [20, 20, 20, 20, 20] } } });
    expect(isAgentsTurn(reveal, "beta")).toBe(true);
    expect(isAgentsTurn(reveal, "alpha")).toBe(false);
  });
  it("a turn game waits on its current player; a finished or foreign game on nobody here", () => {
    const chess = session({ module_type: "emporia:chess:v1", current_agent: "beta" });
    expect(gamesToMove([chess], "beta")).toHaveLength(1);
    expect(gamesToMove([chess], "alpha")).toHaveLength(0);
    expect(isAgentsTurn(session({ status: "completed", state: { phase: "commit" } }), "alpha")).toBe(false);
    expect(isAgentsTurn(session({ participants: ["gamma", "beta"] }), "alpha")).toBe(false);
  });
  it("the chip says the turn first, then unread messages", () => {
    expect(attentionLabel(1, 4)).toEqual({ text: "your turn", urgent: true });
    expect(attentionLabel(2, 0).text).toBe("your turn ×2");
    expect(attentionLabel(0, 3)).toEqual({ text: "3 unread", urgent: false });
    expect(attentionLabel(0, 0).text).toBe("quiet");
  });
});
