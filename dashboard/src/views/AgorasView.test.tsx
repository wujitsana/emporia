// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AgorasView } from "./AgorasView";

// Voting is agents-only: the relay records the voter from the signed-in agent, so a dashboard
// button would cast a vote as the agent. The view shows the scores and offers no way to cast one.

const topics = [
  { topic_id: "t1", slug: "talk", name: "Talk", description: "", visibility: "public", gate_type: "open",
    entry_fee_cents: 0, creator_id: "alice", created_at: "2026-10-01T00:00:00Z", post_count: 1, subscriber_count: 2,
    flair_options: [], host: "home.example", advertised: false },
];
const post = { post_id: "p1", topic_id: "t1", topic_slug: "talk", author_id: "beta", title: "A post", content: "body",
  post_type: "text", flair: null, vote_score: 3, comment_count: 1, is_pinned: false, is_locked: false,
  created_at: "2026-10-02T10:00:00Z" };
const withComment = { ...post, comments: [{ comment_id: "c1", post_id: "p1", parent_comment_id: null, author_id: "alpha",
  content: "a comment", vote_score: -2, created_at: "2026-10-02T11:00:00Z" }] };

vi.mock("../api", () => ({
  api: {
    agoraTopics: vi.fn(() => Promise.resolve({ topics, count: topics.length })),
    agoraTopicsElsewhere: vi.fn(() => Promise.resolve({ topics: [], count: 0 })),
    agoraPosts: vi.fn(() => Promise.resolve({ posts: [post], count: 1 })),
    agoraFeed: vi.fn(() => Promise.resolve({ posts: [] })),
    agoraPost: vi.fn(() => Promise.resolve(withComment)),
  },
}));
vi.mock("../relayEnv", () => ({ RELAY: "http://relay.test", VIEWER_ID: "" }));

afterEach(cleanup);

// A vote control would be named for voting, or be nothing but an arrow (and perhaps a count).
// (A post row is a button too, and holds its score beside its title: that is not a control.)
const VOTE = /vote|like|^\s*[▲▼+-]\s*\d*\s*$/i;
const voteControls = () => screen.queryAllByRole("button").filter((b) => VOTE.test(b.textContent ?? "")
  || VOTE.test(b.getAttribute("aria-label") ?? "") || VOTE.test(b.getAttribute("title") ?? ""));

describe("agoras view votes", () => {
  it("shows a post's score and has no control to vote on it", async () => {
    render(<AgorasView />);
    expect(await screen.findByText("A post")).toBeTruthy();
    expect(screen.getByText("▲ 3")).toBeTruthy();
    expect(voteControls()).toHaveLength(0);
  });

  it("shows the scores of a post and its comments, and still no vote control", async () => {
    render(<AgorasView />);
    fireEvent.click(await screen.findByText("A post"));
    expect(await screen.findByText("a comment")).toBeTruthy();
    await waitFor(() => expect(screen.getByText(/score 3/)).toBeTruthy());
    expect(screen.getByText("▼ 2")).toBeTruthy();
    expect(voteControls()).toHaveLength(0);
    expect(screen.queryAllByRole("spinbutton")).toHaveLength(0);
    expect(screen.getByText(/read-only/)).toBeTruthy();
  });
});
