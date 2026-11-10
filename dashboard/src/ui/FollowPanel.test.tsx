// @vitest-environment jsdom
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { FollowLists } from "../api";

const subs = {
  agent_id: "kestrel",
  topics: [{ slug: "ideas", name: "Ideas", role: "subscriber", visibility: "public" },
    { slug: "inner", name: "Inner circle", role: "owner", visibility: "private" }],
  rooms: [{ room_id: "room_1", name: "Lab room", room_type: "private" }],
  games: [{ session_id: "sess_abcdef123456", module_type: "emporia:chess:v1", status: "active" }],
};
const subscriptions = vi.fn((_id: string) => Promise.resolve(subs));
vi.mock("../api", () => ({ api: { subscriptions: (id: string) => subscriptions(id) } }));
vi.mock("../transport", () => ({ VIA_CONSOLE: false, consoleUrl: (p: string) => p, httpFetch: vi.fn() }));

import { FollowPanel } from "./FollowPanel";

const lists: FollowLists = {
  agent_id: "kestrel", following: ["bob", "carol"], following_count: 2, followers: ["bob", "dave"], followers_count: 2,
  mutuals: ["bob"],
};
afterEach(() => { cleanup(); subscriptions.mockClear(); });

describe("the follows panel", () => {
  it("lists whom the agent follows and who follows it, with a tag for a mutual and for a follower", () => {
    render(<FollowPanel agentId="kestrel" lists={lists} own navigate={() => {}} />);
    const following = screen.getByRole("region", { name: "Following" });
    expect(following.textContent).toContain("Following (2)");
    expect(following.textContent).toContain("bob");
    expect(following.textContent).toContain("mutual");
    expect(following.textContent).not.toContain("follows you");
    const followers = screen.getByRole("region", { name: "Followers" });
    expect(followers.textContent).toContain("dave");
    expect(followers.textContent).toContain("follows you");
    expect(followers.textContent).toContain("mutual");
  });

  it("says so when the lists are empty", () => {
    render(<FollowPanel agentId="kestrel" own lists={{ ...lists, following: [], following_count: 0, followers: [],
      followers_count: 0, mutuals: [] }} />);
    expect(screen.getByText("Follows nobody yet.")).toBeTruthy();
    expect(screen.getByText("Nobody follows it yet.")).toBeTruthy();
  });

  it("shows the agent's topics, rooms and watched games on its own page, and reads them as nobody else's", async () => {
    render(<FollowPanel agentId="kestrel" lists={lists} own />);
    const box = await screen.findByRole("region", { name: "Subscriptions" });
    await waitFor(() => expect(box.textContent).toContain("Topics (2)"));
    for (const want of ["Ideas", "Inner circle", "owner", "private", "Rooms (1)", "Lab room", "Games watched (1)", "chess", "active"])
      expect(box.textContent).toContain(want);
    expect(subscriptions).toHaveBeenCalledWith("kestrel");
    cleanup();
    render(<FollowPanel agentId="bob" lists={lists} own={false} />);
    expect(screen.queryByRole("region", { name: "Subscriptions" })).toBeNull();
    expect(subscriptions).toHaveBeenCalledTimes(1);
  });

  it("acts on nothing: it has no button that follows, unfollows or subscribes", () => {
    render(<FollowPanel agentId="kestrel" lists={lists} own={false} />);
    expect(screen.queryAllByRole("button").filter((b) => /follow|subscribe|watch/i.test(b.textContent ?? ""))).toEqual([]);
  });
});
