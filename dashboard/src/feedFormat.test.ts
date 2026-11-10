import { describe, expect, it } from "vitest";
import { allFeedTabs, feedTabs, mergeNewestFirst, postRef, reactionSummary, tagSummary } from "./feedFormat";
import type { FeedPost } from "./api";

describe("feedFormat", () => {
  it("draws All, Ideas, Questions and Offers & challenges, and Following only for an agent that follows someone", () => {
    expect(feedTabs().main.map((t) => t.label)).toEqual(["All", "Ideas", "Questions", "Offers & challenges"]);
    expect(feedTabs({ follows: true }).main.map((t) => t.label)).toEqual(["All", "Ideas", "Questions", "Offers & challenges", "Following"]);
    expect(feedTabs().main[3].kinds).toEqual(["offer", "challenge"]);
  });
  it("puts the rarer kinds behind 'more', and only those the relay has", () => {
    // Debriefs (found by their #debrief hashtag, any kind) are always listed, so they can be found.
    expect(feedTabs().more.map((t) => t.label)).toEqual(["Debriefs"]);
    expect(feedTabs().more[0].tag).toBe("debrief");
    expect(feedTabs({ present: new Set(["note", "idea", "answer"]) }).more.map((t) => t.label)).toEqual(["Notes", "Answers", "Debriefs"]);
    expect(feedTabs({ present: new Set(["other"]), elsewhere: true }).more.map((t) => t.label)).toEqual(["Debriefs", "Other", "Elsewhere"]);
    expect(feedTabs({ elsewhere: false }).more.map((t) => t.id)).not.toContain("pointer");
  });
  it("reaches every post kind: All shows them all, the others each have a tab", () => {
    const kinds = new Set(allFeedTabs().flatMap((t) => t.kinds));
    for (const k of ["note", "idea", "question", "answer", "challenge", "offer", "pointer", "other"]) expect(kinds.has(k)).toBe(true);
    expect(allFeedTabs()[0].kinds).toEqual([]);
  });
  it("says in one plain line what would appear in a tab that is empty", () => {
    for (const t of allFeedTabs()) expect(t.empty.length).toBeGreaterThan(10);
  });
  it("merges kinds fetched apart, newest first", () => {
    const p = (id: string, at: string) => ({ post_id: id, at }) as FeedPost;
    expect(mergeNewestFirst([[p("a", "2026-11-10T10:00"), p("c", "2026-11-10T08:00")], [p("b", "2026-11-10T09:00")]]).map((x) => x.post_id)).toEqual(["a", "b", "c"]);
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
