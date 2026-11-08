import { describe, expect, it } from "vitest";
import { feedTabs, postRef, reactionSummary, tagSummary } from "./feedFormat";

describe("feedFormat", () => {
  it("has a tab per kind, All first", () => {
    const tabs = feedTabs();
    expect(tabs[0].label).toBe("All");
    expect(tabs.map((t) => t.kind)).toEqual(["", "idea", "question", "challenge", "offer", "pointer", "other"]);
  });
  it("renders tags and refs as text, unknown tags as written", () => {
    expect(
      tagSummary([["t", "games"], ["ref", "game", "g1", "r.example"], ["tool", "mcp-server", "files"], ["zzz", "a", "b"]]),
    ).toBe("#games ref game:g1@r.example tool mcp-server/files zzz a b");
    expect(postRef("pst2_x")).toBe("post:pst2_x");
  });
  it("counts likes first, other values beside them", () => {
    expect(reactionSummary({ likes: 2, reactions: { funny: 1 } })).toBe("2 likes · funny 1");
  });
});
