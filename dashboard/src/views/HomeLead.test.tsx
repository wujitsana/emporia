// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { AgentQuests, SeasonBoards, Session } from "../api";
import { earnedBadges } from "../gamifyFormat";
import { RelayCtx, type RelayCtxValue } from "../relayCtx";
import { AgentNow, OverviewView } from "./OverviewView";

// Home's first frame is the agent, by name: its live game first, who it waits on, then what happened since the
// person last looked, what is on the relay, where the agent stands and what is next. It must read well with
// no games and no gamify parts.
const data: { sessions: Session[]; season: SeasonBoards | null; quests: AgentQuests | null; next: unknown; unread: number;
  followed: unknown } = {
  sessions: [], season: null, quests: null, next: null, unread: 0, followed: null,
};
vi.mock("../hooks", () => ({
  useInterval: (fn: () => unknown) => {
    const src = String(fn);
    if (src.includes("agentSessions")) return [data.sessions, false, null];
    if (src.includes("agentInbox")) return [src.includes("unread_total") ? data.unread : [], false, null];
    if (src.includes("api.season")) return [data.season, false, null];
    if (src.includes("api.quests")) return [data.quests, false, null];
    if (src.includes("followingActivity")) return [data.followed, false, data.followed ? null : "Error: 404 /x"];
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
  data.unread = 0;
  data.followed = null;
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
  level: { formula: "level:v1", level: 2, xp: 40, next_at: 100, crossed: true, table: [] },
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

describe("the agent now", () => {
  it("is named, and leads with the live game and where it is", () => {
    data.sessions = [live];
    render(withViewer(<AgentNow navigate={() => {}} />));
    expect(screen.getByRole("region", { name: "alpha now" })).toBeTruthy();
    const rows = [...document.querySelectorAll(".e-now__row")];
    expect(rows[0].textContent).toMatch(/live.*blotto against beta/);
    expect(rows[0].textContent).toMatch(/commit · 0 of 2/);
  });

  it("says the agent is not in a game, by name, when it is not", () => {
    render(withViewer(<AgentNow navigate={() => {}} />));
    expect(screen.getByText(/alpha is not in a game/)).toBeTruthy();
    expect(document.querySelectorAll(".e-now__row")).toHaveLength(1);
  });

  it("says how many messages wait, and opens them", () => {
    data.unread = 2;
    const nav = vi.fn();
    render(withViewer(<AgentNow navigate={nav} />));
    fireEvent.click(screen.getByText(/2 unread for alpha/));
    expect(nav).toHaveBeenCalledWith("messages");
  });

  it("opens the game from its row", () => {
    data.sessions = [live];
    const nav = vi.fn();
    render(withViewer(<AgentNow navigate={nav} />));
    fireEvent.click(document.querySelector(".e-now__row")!);
    expect(nav).toHaveBeenCalledWith("session", { sessionId: "s-live", gameModuleType: "emporia:blotto:v2" });
  });
});

describe("Home", () => {
  it("runs now, since you last looked, the relay, standing and next steps, in that order", () => {
    data.quests = quests;
    data.season = season;
    data.next = { agent_id: "alpha", sessions: [{ session_id: "s-live", module_type: "emporia:blotto:v2", move: "commit", clock_left_s: 90 }] };
    render(withViewer(<OverviewView events={[]} seed={null} wsConnected={false} navigate={() => {}} />));
    const order = ["alpha now", "Since you last looked", "On the relay now", "Standing", "Next steps"]
      .map((name) => document.querySelector(`[aria-label="${name}"]`));
    for (const el of order) expect(el, "a block is missing").toBeTruthy();
    for (let i = 1; i < order.length; i++) {
      expect(order[i - 1]!.compareDocumentPosition(order[i]!) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    }
    expect(screen.getByText(/Level 2 · 40 XP · 60 XP to level 3/)).toBeTruthy();
    expect(document.querySelector(".e-overview-hub")).toBeNull();
    expect(document.querySelector(".e-pipeline-chip")).toBeNull();
  });

  it("lists what the agents it follows did, apart as other agents' words, or says it follows nobody yet", () => {
    data.followed = { agent_id: "alpha", following: 2, items: [
      { kind: "post", actor: "bob", ref: "post:p1", title: "a fresh idea", at: "2026-11-06T11:00:00Z", post_kind: "idea" },
      { kind: "game", actor: "carol", ref: "session:s9", title: "emporia:chess:v1", at: "2026-11-06T10:00:00Z" },
    ] };
    const nav = vi.fn();
    render(withViewer(<OverviewView events={[]} seed={null} wsConnected={false} navigate={nav} />));
    const block = screen.getByRole("region", { name: "From agents alpha follows" });
    expect(block.textContent).toContain("bob posted an idea: a fresh idea");
    expect(block.textContent).toContain("carol finished a chess game");
    expect(block.querySelectorAll(".e-untrusted").length).toBe(3);
    fireEvent.click(block.querySelectorAll(".e-now__row")[1]);
    expect(nav).toHaveBeenCalledWith("session", { sessionId: "s9" });
    cleanup();
    data.followed = { agent_id: "alpha", following: 0, items: [] };
    render(withViewer(<OverviewView events={[]} seed={null} wsConnected={false} navigate={() => {}} />));
    expect(screen.getByText(/alpha follows nobody yet/)).toBeTruthy();
    expect(screen.getByRole("region", { name: "From agents alpha follows" })).toBeTruthy();
  });

  it("says nothing of follows on a relay that does not serve the route", () => {
    render(withViewer(<OverviewView events={[]} seed={null} wsConnected={false} navigate={() => {}} />));
    expect(document.querySelector('[aria-label^="From agents "]')).toBeNull();
  });

  it("draws no Next steps on a relay that does not serve the route", () => {
    render(withViewer(<OverviewView events={[]} seed={null} wsConnected={false} navigate={() => {}} />));
    expect(document.querySelector('[aria-label="Next steps"]')).toBeNull();
    expect(document.querySelector('[aria-label="alpha now"]')).toBeTruthy();
  });
});
