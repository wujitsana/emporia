// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { FeedView } from "./FeedView";
import type { FeedPost } from "../api";

// The Feed is a grid of the page's cards. A post of kind "other" names itself with a label (the protocol
// requires one): the card says it, and the labels in the feed are listed under "More" in the topic filter.

const post = (id: string, kind: string, label: string, text: string, author = "bob"): FeedPost => ({
  post_id: id, kind, label, text, tags: [], topic: "ideas", reply_to: null, quote_of: null, root: null,
  at: "2026-11-10T10:00:00Z", author_id: author, author_address: `${author}@relay.example`, origin_relay: "r",
  elsewhere: false, likes: 1, reactions: {}, replies: 0, tool_marks: [],
});
const POSTS = [
  post("a", "idea", "", "Rank cards by wins"),
  post("b", "other", "digest", "Weekly digest: 14 games finished", "dave"),
  post("c", "other", "proposal", "A shared glossary of game terms", "erin"),
  post("d", "other", "digest", "Another digest line", "carol"),
];

vi.mock("../api", () => ({
  api: {
    feed: vi.fn((p?: { kind?: string }) => {
      const list = p?.kind ? POSTS.filter((x) => x.kind === p.kind) : POSTS;
      return Promise.resolve({ posts: list, count: list.length, untrusted: "x" });
    }),
    follows: vi.fn(() => Promise.resolve({ following_count: 0 })),
    postThread: vi.fn(() => Promise.resolve({ thread: [] })),
  },
}));
vi.mock("../relayEnv", () => ({ RELAY: "http://relay.test", VIEWER_ID: "" }));

beforeEach(() => { try { sessionStorage.clear(); } catch { /* none */ } });
afterEach(cleanup);

const openMore = () => {
  const summary = screen.getByText(/^More/);
  fireEvent.click(summary);
  return summary.closest("details")!;
};

describe("the feed as cards", () => {
  it("draws each post as one of the page's cards, with its author, its text and its topic", async () => {
    render(<FeedView />);
    await screen.findByText("Rank cards by wins");
    expect(document.querySelectorAll(".e-card-grid > .e-hcard")).toHaveLength(4);
    const first = document.querySelector(".e-card-grid > .e-hcard")!;
    expect(first.textContent).toContain("bob");
    expect(first.textContent).toContain("ideas");
    // one card, not a counted line: the replies and likes ride in its foot
    expect(first.textContent).toContain("1 like");
    expect(first.textContent).not.toContain("0 replies");
  });

  it("says what a post of kind 'other' is: its label, on the card", async () => {
    render(<FeedView />);
    await screen.findByText("Weekly digest: 14 games finished");
    const cards = [...document.querySelectorAll(".e-hcard")];
    const digest = cards.find((c) => c.textContent?.includes("Weekly digest"))!;
    // the label is the headline; "other" is plumbing and is never drawn as a chip
    expect(digest.textContent).not.toMatch(/\bOTHER\b/);
    expect(digest.querySelector(".e-hcard__head .e-sr")?.textContent ?? digest.querySelector(".e-hcard__head")?.textContent).toMatch(/digest/i);
  });

  it("lists the labels the feed has, once each, under More, and filters by one", async () => {
    render(<FeedView />);
    await screen.findByText("Rank cards by wins");
    const menu = openMore();
    const items = [...menu.querySelectorAll("button")].map((b) => b.textContent);
    expect(items).toEqual(expect.arrayContaining(["Other", "digest", "proposal"]));
    expect(items.filter((i) => i === "digest")).toHaveLength(1);
    fireEvent.click([...menu.querySelectorAll("button")].find((b) => b.textContent === "digest")!);
    await waitFor(() => expect(document.querySelectorAll(".e-card-grid > .e-hcard")).toHaveLength(2));
    expect(screen.queryByText("A shared glossary of game terms")).toBeNull();
    expect(screen.getByText("Another digest line")).toBeTruthy();
  });
});
