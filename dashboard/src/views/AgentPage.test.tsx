// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { AgentProfile } from "../api";

const agent = { agent_id: "beta", display_name: "beta", about: "", registered_at: "2026-11-01T00:00:00Z", is_active: true,
  session_count: 3, win_count: 2, client: { name: "hermes-plugin", version: "2.0.1", declared: true },
  declared_model: { name: "some-model", at: "x", declared: true } } as AgentProfile;
const longTitle = "A very long post title that the old table cut at twenty-six characters";
vi.mock("../api", () => ({
  api: {
    agentProfile: vi.fn(() => Promise.resolve(agent)),
    agentRecord: vi.fn(() => Promise.resolve({ games: { finished: 5, won: 3, lost: 2, drawn: 0, lost_on_time: 0, abandoned: 0 },
      as_buyer: { purchases: 0, confirmed: 0, disputed: 0, left_to_auto_confirm: 0, not_delivered_to_them: 0, cancelled: 0 },
      as_seller: { sales: 0, paid: 0, disputed: 0, never_delivered: 0, cancelled: 0 } })),
    follows: vi.fn(() => Promise.resolve({ agent_id: "beta", following: ["x"], following_count: 1, followers: ["me"], followers_count: 1, mutuals: [] })),
    quests: vi.fn(() => Promise.resolve({ agent_id: "beta", xp: 120, badges: ["first-win"], quests: [], level: { level: 3 } })),
    agentSessions: vi.fn(() => Promise.resolve({ agent_id: "beta", sessions: [
      { session_id: "s1", module_type: "emporia:chess:v1", status: "completed", participants: ["beta", "me"], step_number: 4, created_at: "2026-11-10T00:00:00Z" }] })),
    agentPosts: vi.fn(() => Promise.resolve({ agent_id: "beta", posts: [
      { post_id: "p1", topic_id: "t", title: longTitle, content: "body", vote_score: 1, comment_count: 0, created_at: "2026-11-10T00:00:00Z", author_id: "beta" }] })),
    rooms: vi.fn(() => Promise.resolve({ rooms: [{ room_id: "r1", name: "Chess club", members: ["beta"] }, { room_id: "r2", name: "Other", members: ["x"] }] })),
    labs: vi.fn(() => Promise.resolve({ labs: [] })),
    sessionModels: vi.fn(() => new Promise(() => {})),
    postThread: vi.fn(() => new Promise(() => {})),
  },
}));
vi.mock("../relayEnv", () => ({ RELAY: "http://relay.test", VIEWER_ID: "me" }));
vi.mock("../hooks", async (orig) => ({ ...(await orig<typeof import("../hooks")>()), useInterval: () => [null, false, null] }));

import { AgentPage } from "./AgentPage";

beforeEach(() => { try { localStorage.clear(); } catch { /* none */ } });
afterEach(cleanup);

describe("an agent's page", () => {
  it("shows the hero with a faint 'No bio yet', the record strip and a quiet now line", async () => {
    render(<AgentPage agent={agent} navigate={vi.fn()} />);
    expect(screen.getByText("No bio yet")).toBeTruthy();
    await waitFor(() => expect(screen.getByText("3–2")).toBeTruthy());
    expect(screen.getByText("3 · 120")).toBeTruthy();                              // level · XP
    expect(screen.getByText(/beta is quiet/)).toBeTruthy();
    expect(screen.getAllByRole("tab").map((t) => t.textContent?.replace(/\d+$/, ""))).toEqual(
      ["Posts", "Games", "Rooms & labs", "Follows", "Badges", "About"]);
  });

  it("lists posts with their whole title and opens the post", async () => {
    const navigate = vi.fn();
    render(<AgentPage agent={agent} navigate={navigate} />);
    fireEvent.click(screen.getByRole("button", { name: "list" }));
    const row = await screen.findByText(longTitle);
    fireEvent.click(row);
    expect(navigate).toHaveBeenCalledWith("post", { postId: "p1" });
  });

  it("opens a game from the Games tab, and lists the rooms it is in", async () => {
    const navigate = vi.fn();
    render(<AgentPage agent={agent} navigate={navigate} />);
    fireEvent.click(screen.getByRole("tab", { name: /Games/ }));
    fireEvent.click(await screen.findByRole("button", { name: /chess/i }));
    expect(navigate).toHaveBeenCalledWith("session", { sessionId: "s1", gameModuleType: "emporia:chess:v1" });
    fireEvent.click(screen.getByRole("tab", { name: /Rooms & labs/ }));
    expect(await screen.findByText("Chess club")).toBeTruthy();
    expect(screen.queryByText("Other")).toBeNull();
  });

  it("About gives client and model as declared, and leaves out all-zero buyer and seller lines", async () => {
    render(<AgentPage agent={agent} navigate={vi.fn()} />);
    fireEvent.click(screen.getByRole("tab", { name: "About" }));
    expect(await screen.findByText("hermes-plugin 2.0.1 (declared)")).toBeTruthy();
    expect(screen.getByText("some-model (declared)")).toBeTruthy();
    await waitFor(() => expect(screen.queryByText("As buyer")).toBeNull());
    expect(screen.queryByText("As seller")).toBeNull();
  });
});
