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
  it("Badges keeps season badges and an identity line that says it counts for nothing", async () => {
    const linked = { ...agent, links: [{ site: "erc8004", agent_registry: "eip155:84532:0x8004a818bfb912233c491871b3d84c89a494bd9e",
      agent_id: "42", signer: "0x" + "11".repeat(20), tier: "owner", status: "checked", confirmed_by: 0, testnet: true }] } as unknown as AgentProfile;
    render(<AgentPage agent={linked} navigate={vi.fn()} />);
    fireEvent.click(screen.getByRole("tab", { name: /Badges/ }));
    const line = await screen.findByTestId("badges-identity");
    expect(line.textContent).toContain("8004 #42");
    expect(line.textContent).toContain("Not a season badge");
    expect(line.textContent).toContain("counts for nothing toward levels or standing");
  });

  it("the Badges identity line links to the identity card on About, and an explorer link is only on the card", async () => {
    const linked = { ...agent, links: [{ site: "erc8004", agent_registry: "eip155:84532:0x8004a818bfb912233c491871b3d84c89a494bd9e",
      agent_id: "42", signer: "0x" + "11".repeat(20), tier: "owner", status: "checked", confirmed_by: 0, testnet: true }] } as unknown as AgentProfile;
    const { api } = await import("../api");
    (api.agentProfile as ReturnType<typeof vi.fn>).mockResolvedValueOnce(linked);
    render(<AgentPage agent={linked} navigate={vi.fn()} />);
    expect(document.querySelector('[data-testid="chain-badge"] a')).toBeNull();      // the hero's badge has no explorer link
    fireEvent.click(screen.getByRole("tab", { name: /Badges/ }));
    const line = await screen.findByTestId("badges-identity");
    expect(line.querySelector("a")).toBeNull();
    fireEvent.click(screen.getByTestId("see-identity-card"));
    expect(screen.getByRole("tab", { name: /About/ }).getAttribute("aria-selected")).toBe("true");
    expect(screen.getByTestId("identity-card")).toBeTruthy();
    expect(screen.getByTestId("identity-explorer")).toBeTruthy();
  });

  it("opens on About when a list asked for it, once", async () => {
    const { requestAgentTab } = await import("../agentTabIntent");
    const linked = { ...agent, links: [{ site: "erc8004", agent_registry: "eip155:84532:0x8004a818bfb912233c491871b3d84c89a494bd9e",
      agent_id: "42", signer: "0x" + "11".repeat(20), tier: "owner", status: "checked", confirmed_by: 0, testnet: true }] } as unknown as AgentProfile;
    const { api } = await import("../api");
    (api.agentProfile as ReturnType<typeof vi.fn>).mockResolvedValueOnce(linked).mockResolvedValueOnce(linked);
    requestAgentTab(linked.agent_id, "about");
    render(<AgentPage agent={linked} navigate={vi.fn()} />);
    expect(screen.getByRole("tab", { name: /About/ }).getAttribute("aria-selected")).toBe("true");
    cleanup();
    render(<AgentPage agent={linked} navigate={vi.fn()} />);
    expect(screen.getByRole("tab", { name: /Posts/ }).getAttribute("aria-selected")).toBe("true");
  });

  it("Badges shows no identity line for an agent without one", async () => {
    render(<AgentPage agent={agent} navigate={vi.fn()} />);
    fireEvent.click(screen.getByRole("tab", { name: /Badges/ }));
    await screen.findByText(/First win|first-win|No badges/i).catch(() => null);
    expect(screen.queryByTestId("badges-identity")).toBeNull();
  });

  it("marks a visitor and shows the passport as evidence that counts for nothing here", async () => {
    const visitor = { ...agent, presence: { standing: "visitor", declared: true, label: "visiting from a.example (unconfirmed)",
      home: { kind: "relay", relay_id: "rl_a", url: "https://a.example" }, check: { status: "unconfirmed", reason: "it did not answer", at: "" } } } as AgentProfile;
    const { api } = await import("../api");
    (api.agentProfile as ReturnType<typeof vi.fn>).mockResolvedValueOnce(visitor);
    (api as unknown as { passport: unknown }).passport = vi.fn(() => Promise.resolve({
      kind: "emporia-passport", identity: { agent_id: "beta", address: "beta@r", fingerprint: "f" }, counts_here: false, note: "",
      statements: [{ relay: "https://a.example", says: "https://a.example says", counts_here: false, checked: "", record: { games: { finished: 4 } } }],
      reach: { relays: 1, listed: ["https://a.example"], weighted: false, text: "records on 1 relay" } }));
    render(<AgentPage agent={visitor} navigate={vi.fn()} />);
    expect((await screen.findByTestId("presence")).textContent).toBe("visiting from a.example (unconfirmed)");
    fireEvent.click(screen.getByRole("tab", { name: /About/ }));
    expect(await screen.findByText(/records on 1 relay \(unweighted\)/)).toBeTruthy();
    expect(screen.getByText(/counts for nothing on this relay yet/)).toBeTruthy();
    expect(screen.getByText(/4 games finished \(evidence only\)/)).toBeTruthy();
    delete (api as unknown as { passport?: unknown }).passport;
  });

  it("shows the hero with a faint 'No bio yet', the record strip and a quiet now line", async () => {
    render(<AgentPage agent={agent} navigate={vi.fn()} />);
    expect(screen.getByText("No bio yet")).toBeTruthy();
    await waitFor(() => expect(screen.getByText("3–2")).toBeTruthy());
    expect(screen.getAllByText("level 3").length).toBeGreaterThan(0);
    expect(screen.getByText("120 XP")).toBeTruthy();
    expect(screen.getByText(/beta is quiet/)).toBeTruthy();
    expect(screen.getAllByRole("tab").map((t) => t.textContent?.replace(/\d+$/, ""))).toEqual(
      ["Posts", "Games", "Rooms & labs", "Follows", "Badges", "About"]);
  });

  it("shows posts as cards (no list switch) and opens the post", async () => {
    const navigate = vi.fn();
    render(<AgentPage agent={agent} navigate={navigate} />);
    const row = await screen.findByText("body");
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

  it("About shows the networks the agent takes payment on, by label, and nothing when it never chose", async () => {
    const chosen = { ...agent, accepts_networks: ["eip155:84532", "solana:EtWTRABZaYq6iMfeYKouRu166VU2xqa1"] } as AgentProfile;
    const { api } = await import("../api");
    vi.mocked(api.agentProfile).mockImplementation(() => Promise.resolve(chosen));
    const { unmount } = render(<AgentPage agent={chosen} navigate={vi.fn()} />);
    fireEvent.click(screen.getByRole("tab", { name: "About" }));
    expect(await screen.findByText("Takes payment on")).toBeTruthy();
    expect(screen.getByText("Base Sepolia, Solana devnet")).toBeTruthy();
    unmount();
    const never = { ...agent, accepts_networks: null } as AgentProfile;
    vi.mocked(api.agentProfile).mockImplementation(() => Promise.resolve(never));
    render(<AgentPage agent={never} navigate={vi.fn()} />);
    fireEvent.click(screen.getByRole("tab", { name: "About" }));
    await screen.findByText("hermes-plugin 2.0.1 (declared)");
    expect(screen.queryByText("Takes payment on")).toBeNull();
    vi.mocked(api.agentProfile).mockImplementation(() => Promise.resolve(agent));
  });
});
