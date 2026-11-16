// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import type { FeedPost } from "../api";
import { ThreadReplies } from "./ThreadReplies";

afterEach(cleanup);

const reply = (over: Partial<FeedPost>): FeedPost => ({
  post_id: "p1", kind: "note", label: "", text: "hello", tags: [], topic: null, reply_to: "post:root", root: "post:root",
  at: "2026-11-16T00:00:00Z", author_id: "carol@c-relay.test", author_address: "carol@c-relay.test", origin_relay: "http://c-relay.test",
  elsewhere: true, likes: 0, reactions: {}, replies: 0, tool_marks: [], ...over,
} as FeedPost);

describe("a reply by an author whose key is not pinned here", () => {
  it("shows the relay's note quietly beside the author", () => {
    const note = "via b-relay.test, author key not pinned here";
    render(<ThreadReplies rootId="root" replies={[reply({ author_unverified: true, author_note: note })]} />);
    expect(screen.getByTestId("author-note").textContent).toBe(note);
  });

  it("shows nothing for an ordinary reply", () => {
    render(<ThreadReplies rootId="root" replies={[reply({})]} />);
    expect(screen.queryByTestId("author-note")).toBeNull();
  });
});
