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
    seasonBoard: vi.fn(() => Promise.resolve({ standings: [{ rank: 1, agent_id: "beta", season_xp: 5, level: 3, badges: 0 }] })),
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

describe("the agents list names the handle first", () => {
  it("shows the handle as the row's name and the display name after it, fainter", async () => {
    agentsMock.mockResolvedValue({
      agents: [mk("alpha"), { ...mk("x7q"), display_name: "wujitsаna" }], count: 2, has_more: false });
    const { container } = render(<AgentsView />);
    await waitFor(() => expect(screen.getByText(/x7q/)).toBeTruthy());
    const dn = container.querySelector("[data-agent-display-name]") as HTMLElement;
    expect(dn.textContent).toContain("wujitsаna");
    expect(dn.className).toContain("e-faint");
    const row = dn.parentElement as HTMLElement;
    expect(row.textContent!.indexOf("x7q")).toBeLessThan(row.textContent!.indexOf("wujitsаna"));
    expect(container.querySelectorAll("[data-agent-display-name]").length).toBe(1);   // alpha has none to repeat
  });
});

describe("the agents directory opens an agent's page", () => {
  it("opens the page through navigate, shows a level where the season board has one, and the bio line", async () => {
    agentsMock.mockResolvedValue({ agents: [mk("alpha"), mk("beta", "plays bargain")], count: 2, has_more: false });
    const navigate = vi.fn();
    render(<AgentsView navigate={navigate} />);
    await waitFor(() => expect(screen.getByText("beta")).toBeTruthy());
    expect(screen.getByText("plays bargain")).toBeTruthy();
    await waitFor(() => expect(screen.getByText("level 3")).toBeTruthy());
    fireEvent.click(screen.getByText("beta"));
    expect(navigate).toHaveBeenCalledWith("agents", { agentId: "beta" });
  });
  it("lists or cards, and never repeats the client version on a row", async () => {
    agentsMock.mockResolvedValue({ agents: [{ ...mk("beta"), client: { name: "hermes-plugin", version: "2.0.1", declared: true } }], count: 1, has_more: false });
    render(<AgentsView navigate={vi.fn()} />);
    await waitFor(() => expect(screen.getByText("beta")).toBeTruthy());
    expect(screen.queryByText(/hermes-plugin/)).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "list" }));
    expect(screen.getByRole("list", { name: "Agents" })).toBeTruthy();
    expect(screen.queryByText(/hermes-plugin/)).toBeNull();
  });
});
