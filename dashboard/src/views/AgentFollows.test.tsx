// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AgentDetailPanel } from "./AgentsView";
import type { AgentProfile, FollowLists } from "../api";

// An agent's page says how many follow it and whom it follows, tags it when it follows the agent the person
// views as ("follows me" / "mutual"), and lists both in its "follows" tab. It only reads.
let lists: FollowLists = {
  agent_id: "a1", following: ["me", "carol"], following_count: 2, followers: ["dave"], followers_count: 1, mutuals: [],
};
vi.mock("../api", () => ({
  api: {
    agentProfile: vi.fn(() => new Promise(() => {})),
    agentSessions: vi.fn(), agentPosts: vi.fn(),
    agentRecord: vi.fn(() => new Promise(() => {})),
    follows: vi.fn(() => Promise.resolve(lists)),
    subscriptions: vi.fn(() => Promise.reject(new Error("403"))),
  },
}));
vi.mock("../relayEnv", () => ({ RELAY: "http://relay.test", VIEWER_ID: "me" }));

const agent = { agent_id: "a1", display_name: "Agent One", registered_at: new Date().toISOString(), is_active: true,
  session_count: 0, win_count: 0 } as AgentProfile;
afterEach(cleanup);

describe("another agent's page", () => {
  it("counts its followers and followings and says it follows the viewing agent", async () => {
    render(<AgentDetailPanel agent={agent} />);
    const counts = await screen.findByTestId("follow-counts");
    expect(counts.textContent).toContain("2 following · 1 follower");
    expect(counts.textContent).toContain("follows me");
    expect(counts.textContent).not.toContain("mutual");
  });

  it("says mutual when both follow, and lists both in the follows tab", async () => {
    lists = { ...lists, followers: ["me", "dave"], followers_count: 2, mutuals: ["me"] };
    render(<AgentDetailPanel agent={agent} />);
    await waitFor(() => expect(screen.getByTestId("follow-counts").textContent).toContain("mutual"));
    fireEvent.click(screen.getByText("follows"));
    const panel = screen.getByTestId("follow-panel");
    expect(panel.textContent).toContain("Following (2)");
    expect(panel.textContent).toContain("Followers (2)");
    expect(screen.queryByRole("region", { name: "Subscriptions" })).toBeNull();     // not its own page: private to the agent
  });
});
