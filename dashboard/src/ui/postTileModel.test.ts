import { describe, expect, it } from "vitest";
import type { PostCardData } from "../postCardModel";
import { hashtagLine, postHead, postSpec } from "./postTileModel";

const base: PostCardData = { post_id: "p", author: "bob", tag: "", kind: "note", text: "hi", at: "", likes: 0, replies: 0 };

// A card says the kind only when it adds something: a note is headed by its author, never NOTE and POST.
describe("a post card's header", () => {
  it("heads a note with its author and gives it no chip", () => {
    const c = postSpec({ ...base, topic_name: "Ideas" });
    expect(postHead(base)).toBe("bob");
    expect(c.head).toBe("bob");
    expect(c.chip).toBeUndefined();
    expect(c.caption).toBe("Ideas");
  });

  it("has no caption for a note with no topic", () => {
    expect(postSpec(base).caption).toBe("");
  });

  it("heads a kind with the kind, and puts the author and topic in the caption", () => {
    const c = postSpec({ ...base, kind: "idea", topic_name: "Ideas" });
    expect(c.head).toBe("idea");
    expect(c.chip).toBeUndefined();
    expect(c.caption).toBe("bob · Ideas");
  });

  it("heads an `other` post with its label and no OTHER chip", () => {
    const c = postSpec({ ...base, kind: "other", label: "digest" });
    expect(c.head).toBe("digest");
    expect(c.chip).toBeUndefined();
  });

  it("names a pointer's tool in the caption", () => {
    const c = postSpec({ ...base, kind: "pointer", tags: [["tool", "hermes-skill", "chess-openings"]] });
    expect(c.caption).toBe("bob · tool hermes-skill/chess-openings");
  });

  it("marks a reply, a sealed post and a debrief", () => {
    expect(postSpec({ ...base, reply_to: "post:q" }).chip).toBe("REPLY");
    expect(postSpec({ ...base, sealed: true }).chip).toBe("SEALED");
    const d = postSpec({ ...base, tags: [["t", "debrief"]], reply_to: "session:sess_chess00000001" });
    expect(d.head).toBe("Debrief");
    expect(d.caption).toContain("game …00000001");
  });

  it("draws only the non-zero parts of the foot", () => {
    expect(postSpec(base).foot).toBe("");
    expect(postSpec({ ...base, replies: 2, likes: 1 }).foot).toBe("2 replies · 1 like");
  });

  it("lists up to three hashtags, then how many more, never the debrief tag", () => {
    expect(hashtagLine([["t", "a"], ["t", "debrief"], ["t", "b"]])).toBe("#a #b");
    expect(hashtagLine([["t", "a"], ["t", "b"], ["t", "c"], ["t", "d"], ["t", "e"]])).toBe("#a #b #c +2");
  });
});
