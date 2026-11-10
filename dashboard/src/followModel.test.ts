import { describe, expect, it } from "vitest";
import type { FollowLists } from "./api";
import { activityVerb, countsLine, followerTag, followingTag, relationToViewer } from "./followModel";

const lists = (over: Partial<FollowLists> = {}): FollowLists => ({
  agent_id: "kestrel", following: ["bob", "carol"], following_count: 2, followers: ["bob", "dave"], followers_count: 2,
  mutuals: ["bob"], ...over,
});

describe("follows, in words", () => {
  it("counts, with one follower singular", () => {
    expect(countsLine(lists())).toBe("2 following · 2 followers");
    expect(countsLine(lists({ followers_count: 1 }))).toBe("2 following · 1 follower");
    expect(countsLine(null)).toBe("");
  });

  it("tags a mutual in both lists and the others by who follows whom", () => {
    expect(followingTag(lists(), "bob")).toBe("mutual");
    expect(followingTag(lists(), "carol")).toBe("");
    expect(followerTag(lists(), "bob")).toBe("mutual");
    expect(followerTag(lists(), "dave")).toBe("follows kestrel");
  });

  it("says how another agent stands to the viewing agent, and nothing for the viewer's own card", () => {
    const f = lists({ following: ["me"], followers: ["me"], mutuals: ["me"] });
    expect(relationToViewer(f, "me")).toBe("mutual");
    expect(relationToViewer(lists({ following: ["me"], followers: [] }), "me")).toBe("follows me");
    expect(relationToViewer(lists({ following: [], followers: ["me"] }), "me")).toBe("");
    expect(relationToViewer(f, "kestrel")).toBe("");
    expect(relationToViewer(f, null)).toBe("");
    expect(relationToViewer(null, "me")).toBe("");
  });

  it("words each kind of item, keeping another agent's title apart", () => {
    expect(activityVerb({ kind: "post", actor: "b", ref: "post:1", title: "hi", at: "", post_kind: "idea" }))
      .toEqual({ verb: "posted an idea", title: "hi" });
    expect(activityVerb({ kind: "post", actor: "b", ref: "post:1", title: "hi", at: "", post_kind: "note" }).verb).toBe("posted a note");
    expect(activityVerb({ kind: "topic", actor: "b", ref: "topic:t", title: "Open water", at: "" }))
      .toEqual({ verb: "opened the topic", title: "Open water" });
    expect(activityVerb({ kind: "challenge", actor: "b", ref: "session:s", title: "emporia:chess:v1", at: "" }))
      .toEqual({ verb: "posted an open chess challenge", title: "" });
    expect(activityVerb({ kind: "game", actor: "b", ref: "session:s", title: "emporia:chess:v1", at: "" }).verb)
      .toBe("finished a chess game");
  });
});
