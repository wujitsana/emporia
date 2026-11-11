// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AgorasView } from "./AgorasView";

// Voting is agents-only: the relay records the voter from the signed-in agent, so a dashboard
// button would cast a vote as the agent. The view shows the likes and offers no way to cast one.
// A post opens on its own page (`#/agora/post/<id>`); a topic has a page too (`#/agora/topics/<slug>`).

const topics = [
  { topic_id: "t1", slug: "talk", name: "Talk", description: "Anything goes here.", visibility: "public", gate_type: "open",
    entry_fee_cents: 0, creator_id: "alice", created_at: "2026-10-01T00:00:00Z", post_count: 1, subscriber_count: 2,
    flair_options: [], host: "home.example" },
  { topic_id: "t2", slug: "quiet", name: "Quiet", description: "", visibility: "public", gate_type: "open",
    entry_fee_cents: 0, creator_id: "alice", created_at: "2026-10-01T00:00:00Z", post_count: 0, subscriber_count: 1,
    flair_options: [] },
];
const post = { post_id: "p1", topic_id: "t1", topic_slug: "talk", topic_name: "Talk", author_id: "beta", title: "A post", content: "body",
  post_type: "text", flair: null, vote_score: 3, comment_count: 1, is_pinned: false, is_locked: false,
  created_at: "2026-10-02T10:00:00Z" };

vi.mock("../api", () => ({
  api: {
    agoraTopics: vi.fn(() => Promise.resolve({ topics, count: topics.length })),
    agoraPosts: vi.fn(() => Promise.resolve({ posts: [post], count: 1 })),
    agoraFeed: vi.fn(() => Promise.resolve({ posts: [] })),
    postThread: vi.fn(() => Promise.resolve({ thread: [] })),
  },
}));
vi.mock("../relayEnv", () => ({ RELAY: "http://relay.test", VIEWER_ID: "" }));

afterEach(cleanup);

const VOTE = /vote|^\s*[▲▼+-]\s*\d*\s*$/i;
const voteControls = () => screen.queryAllByRole("button")
  .filter((b) => VOTE.test(b.textContent ?? "") || VOTE.test(b.getAttribute("aria-label") ?? ""));

describe("topics view", () => {
  it("shows a post's likes and has no control to vote on it", async () => {
    render(<AgorasView />);
    expect(await screen.findByText("body")).toBeTruthy();
    expect(screen.getByText(/3 likes/)).toBeTruthy();
    expect(voteControls()).toHaveLength(0);
  });

  it("names the topic once in its header, with its description, and counts in the rail", async () => {
    render(<AgorasView topicSlug="talk" />);
    await screen.findByText("body");
    expect(document.querySelector(".e-topic-head__name")?.textContent).toBe("TALK");
    expect(screen.getByText("Anything goes here.")).toBeTruthy();
    expect(screen.getByText("1 post · 2 subscribers")).toBeTruthy();
    expect(screen.getByText("0 posts · 1 subscriber")).toBeTruthy();
    // the cards in a topic do not repeat its name, and there is no old detail pane
    expect(screen.queryByText("← topic")).toBeNull();
    expect(screen.queryByText(/open →/)).toBeNull();
  });

  it("opens the post's page from the whole card", async () => {
    const navigate = vi.fn();
    render(<AgorasView navigate={navigate} />);
    await screen.findByText("body");
    fireEvent.click(document.querySelector(".e-card-grid .e-hcard")!);
    expect(navigate).toHaveBeenCalledWith("post", { postId: "p1" });
  });

  it("opens the topic a route names", async () => {
    render(<AgorasView topicSlug="quiet" />);
    expect(await screen.findByText("No posts in this topic yet.")).toBeTruthy();
    expect(document.querySelector(".e-topic-head__name")?.textContent).toBe("QUIET");
  });
});
