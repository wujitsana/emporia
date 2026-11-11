import { describe, expect, it } from "vitest";
import { hashForLab, hashForPost, hashForTopic, routeFromHash } from "./dashboardRoute";

// The post page, a topic's page and a lab's page are routes of the Agora: a link opens the thing itself.
describe("post, topic and lab routes", () => {
  it("reads a post page, with the reply to mark", () => {
    expect(routeFromHash("#/agora/post/pst_1")).toMatchObject({ view: "feed", postId: "pst_1", replyId: null });
    expect(routeFromHash("#/agora/post/pst_1?reply=pst_102")).toMatchObject({ view: "feed", postId: "pst_1", replyId: "pst_102" });
  });

  it("ignores a post id that is not an id", () => {
    expect(routeFromHash("#/agora/post/a b").postId).toBeNull();
    expect(routeFromHash("#/agora/post/").postId).toBeNull();
    expect(routeFromHash("#/agora/post/pst_1?reply=<x>").replyId).toBeNull();
  });

  it("reads a topic and a lab by their slug", () => {
    expect(routeFromHash("#/agora/topics/ideas")).toMatchObject({ view: "agora", topicSlug: "ideas" });
    expect(routeFromHash("#/agora/topics")).toMatchObject({ view: "agora", topicSlug: null });
    expect(routeFromHash("#/agora/labs/emporia")).toMatchObject({ view: "labs", labSlug: "emporia" });
  });

  it("writes the routes it reads", () => {
    expect(hashForPost("pst_1")).toBe("#/agora/post/pst_1");
    expect(hashForPost("pst_1", "pst_102")).toBe("#/agora/post/pst_1?reply=pst_102");
    expect(hashForTopic("ideas")).toBe("#/agora/topics/ideas");
    expect(hashForLab("emporia")).toBe("#/agora/labs/emporia");
    expect(routeFromHash(hashForPost("a:b", "c.d")).postId).toBe("a:b");
  });
});
