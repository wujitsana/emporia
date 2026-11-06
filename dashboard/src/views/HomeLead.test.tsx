// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { AgentQuests, SeasonBoards, Session } from "../api";
import { earnedBadges } from "../gamifyFormat";
import { RelayCtx, type RelayCtxValue } from "../relayCtx";
import { AgentNow, OverviewView } from "./OverviewView";

// P2/P3: Home's first frame is the agent — its live game first, its earned badges — then
// what Emporia is. It must read well with no games, no badges, and no gamify parts.
const data: { sessions: Session[]; season: SeasonBoards | null; quests: AgentQuests | null; next: unknown } = {
  sessions: [], season: null, quests: null, next: null,
};
vi.mock("../hooks", () => ({
  useInterval: (fn: () => unknown) => {
    const src = String(fn);
    if (src.includes("agentSessions")) return [data.sessions, false, null];
    if (src.includes("api.season")) return [data.season, false, null];
    if (src.includes("api.quests")) return [data.quests, false, null];
    if (src.includes("agentNext")) return [data.next, false, data.next ? null : "Error: 404 /agents/alpha/next"];
    return [null, true, null];
  },
}));
vi.mock("../relayEnv", async (orig) => ({ ...(await orig<typeof import("../relayEnv")>()), VIEWER_ID: "alpha" }));

beforeEach(() => { vi.stubGlobal("fetch", vi.fn(() => Promise.reject(new Error("offline in this test")))); });
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  data.sessions = [];
  data.season = null;
  data.quests = null;
  data.next = null;
});

const now = Date.parse("2026-11-06T12:00:00Z");
const quests: AgentQuests = {
  agent_id: "alpha", xp: 40, next: "post in #ideas",
  badges: ["hello", "first-set-play", "welcomed"],
  quests: [
    { id: "hello", title: "Say hello", xp: 10, done: true, at: "2026-11-06T09:00:00Z" },
    { id: "first-set-play", title: "Play a set play", xp: 20, done: true, at: "2026-11-01T09:00:00Z" },
    { id: "idea", title: "Suggest an improvement", xp: 10, done: false, at: "" },
  ],
  feats: [],
};
const season: SeasonBoards = {
  season: "season-0", started_at: null, ends_at: null, formula: "season:v1", order: ["renown"],
  boards: { renown: [{ agent_id: "alpha", score: 3, likes: 4 }] }, games: 2, likes: 4,
} as SeasonBoards;
const live: Session = {
  session_id: "s-live", module_type: "emporia:blotto:v2", status: "active", participants: ["alpha", "beta"],
  current_agent: "alpha", step_number: 1, created_at: "2026-11-06T11:00:00Z",
  state: { data: { phase: "commit", commitments: {}, revealed: {}, battlefields: 5, troops: 100 } },
} as Session;

const withViewer = (node: ReactNode) =>
  <RelayCtx.Provider value={{ isSpectator: false, isRelayOperator: false } as RelayCtxValue}>{node}</RelayCtx.Provider>;

describe("earnedBadges", () => {
  it("lists what the Season views count as earned, welcomed first, with a top-three board place", () => {
    const b = earnedBadges(quests, 1, now);
    expect(b.map((x) => x.id)).toEqual(["welcomed", "hello", "first-set-play", "rank-1"]);
    expect(b.find((x) => x.id === "hello")).toEqual({ id: "hello", title: "earned 2026-11-06", fresh: true });
    expect(b.find((x) => x.id === "first-set-play")?.fresh).toBe(false);
  });

  it("is empty with no quests answer, and a place below three is no badge", () => {
    expect(earnedBadges(null, null)).toEqual([]);
    expect(earnedBadges({ ...quests, badges: [] }, 4)).toEqual([]);
  });
});

describe("your agent now", () => {
  it("leads with the live game, then the badges the agent earned", () => {
    data.sessions = [live];
    data.quests = quests;
    data.season = season;
    render(withViewer(<AgentNow navigate={() => {}} />));
    const rows = [...document.querySelectorAll(".e-now__row")];
    expect(rows[0].textContent).toMatch(/live.*blotto v beta/);
    expect(rows[1].classList.contains("e-now__row--badges")).toBe(true);
    expect(screen.getAllByRole("img", { name: "Welcomed" })).toHaveLength(1);
    expect(rows[1].querySelectorAll(".e-badge")).toHaveLength(4);
  });

  it("reads well with no game and no badges yet", () => {
    data.quests = { ...quests, badges: [] };
    render(withViewer(<AgentNow navigate={() => {}} />));
    expect(screen.getByText("Not in a game right now.")).toBeTruthy();
    expect(screen.getByText("None yet: each quest done earns one.")).toBeTruthy();
  });

  it("has no badges row on a relay that runs no gamify parts", () => {
    render(withViewer(<AgentNow navigate={() => {}} />));
    expect(document.querySelector(".e-now__row--badges")).toBeNull();
    expect(screen.getByText("Not in a game right now.")).toBeTruthy();
    expect(document.querySelectorAll(".e-now__row")).toHaveLength(1);
  });

  it("opens the agent's sheet from the badges row", () => {
    data.quests = quests;
    const nav = vi.fn();
    render(withViewer(<AgentNow navigate={nav} />));
    fireEvent.click(document.querySelector(".e-now__row--badges")!);
    expect(nav).toHaveBeenCalledWith("season", { agentId: "alpha" });
  });
});

describe("Home's first frame", () => {
  it("is the agent and what is next, before the intro; the pipeline is one chip", () => {
    data.next = { agent_id: "alpha", sessions: [{ session_id: "s-live", module_type: "emporia:blotto:v2", move: "commit", clock_left_s: 90 }] };
    render(withViewer(<OverviewView events={[]} seed={null} wsConnected={false} navigate={() => {}} />));
    const lead = document.querySelector(".e-home-lead")!;
    const intro = document.querySelector(".e-intro")!;
    expect(lead.compareDocumentPosition(intro) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(lead.querySelector('[aria-label="Your agent now"]')).toBeTruthy();
    expect(lead.querySelector('[aria-label="Next up"]')).toBeTruthy();
    expect(screen.getByText("6 checks on every move").tagName).toBe("SUMMARY");
    expect(document.querySelector(".e-pipeline-strip")).toBeNull();
  });

  it("draws no Next up on a relay that does not serve the route", () => {
    render(withViewer(<OverviewView events={[]} seed={null} wsConnected={false} navigate={() => {}} />));
    expect(document.querySelector('[aria-label="Next up"]')).toBeNull();
    expect(document.querySelector('.e-home-lead [aria-label="Your agent now"]')).toBeTruthy();
  });
});
