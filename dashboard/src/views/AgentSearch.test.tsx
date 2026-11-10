// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { AgentProfile } from "../api";

const mk = (id: string, about = ""): AgentProfile => ({
  agent_id: id, display_name: id, about, registered_at: "2026-11-10T00:00:00Z", is_active: true, session_count: 2, win_count: 1,
} as AgentProfile);

const agentsMock = vi.fn();
vi.mock("../api", () => ({
  api: {
    agents: (q?: string) => agentsMock(q),
    agentProfile: vi.fn(() => new Promise(() => {})),
    agentSessions: vi.fn(() => new Promise(() => {})), agentPosts: vi.fn(() => new Promise(() => {})),
    agentRecord: vi.fn(() => new Promise(() => {})), follows: vi.fn(() => new Promise(() => {})),
    leaderboard: vi.fn(() => new Promise(() => {})),
  },
}));
vi.mock("../relayEnv", () => ({ RELAY: "http://relay.test", VIEWER_ID: "alpha" }));

import { AgentsView } from "./AgentsView";

beforeEach(() => { agentsMock.mockReset(); });
afterEach(cleanup);

describe("the agents list's own search", () => {
  it("filters what is loaded the moment something is typed, on id, name and bio, and asks the relay nothing", async () => {
    agentsMock.mockResolvedValue({ agents: [mk("alpha"), mk("beta", "plays bargain"), mk("rt-probe")], count: 3, has_more: false });
    render(<AgentsView />);
    await waitFor(() => expect(screen.getByText("rt-probe")).toBeTruthy());
    const box = screen.getByLabelText("Search agents");
    fireEvent.change(box, { target: { value: "PROBE" } });
    expect(screen.getByText("rt-probe")).toBeTruthy();
    expect(screen.queryByText("beta")).toBeNull();
    fireEvent.change(box, { target: { value: "bargain" } });
    expect(screen.getByText("beta")).toBeTruthy();
    expect(screen.queryByText("rt-probe")).toBeNull();
    expect(agentsMock).toHaveBeenCalledTimes(1);                 // the one load; the typing was local
  });

  it("says so when nothing matches, and clears with the × button", async () => {
    agentsMock.mockResolvedValue({ agents: [mk("alpha"), mk("beta")], count: 2, has_more: false });
    render(<AgentsView />);
    await waitFor(() => expect(screen.getByText("beta")).toBeTruthy());
    fireEvent.change(screen.getByLabelText("Search agents"), { target: { value: "zzz" } });
    expect(screen.getByText("No agent matches “zzz”.")).toBeTruthy();
    fireEvent.click(screen.getByLabelText("Clear search"));
    expect(screen.getByText("beta")).toBeTruthy();
    expect((screen.getByLabelText("Search agents") as HTMLInputElement).value).toBe("");
  });

  it("asks the relay too, after a pause, only when the loaded list is a cut of a longer one", async () => {
    agentsMock.mockImplementation((q?: string) => Promise.resolve(q
      ? { agents: [mk("far-away")], count: 1, has_more: false }
      : { agents: [mk("alpha"), mk("beta")], count: 2, has_more: true }));
    render(<AgentsView />);
    await waitFor(() => expect(screen.getByText("beta")).toBeTruthy());
    fireEvent.change(screen.getByLabelText("Search agents"), { target: { value: "far" } });
    await act(async () => { await new Promise((r) => setTimeout(r, 450)); });
    await waitFor(() => expect(screen.getByText("far-away")).toBeTruthy());
    expect(agentsMock).toHaveBeenCalledWith("far");
  });
});
