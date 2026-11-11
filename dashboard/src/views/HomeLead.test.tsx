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
  followed: unknown; unreadEvents: unknown[]; events: unknown[]; happening: unknown } = {
  sessions: [], season: null, quests: null, next: null, unread: 0, followed: null, unreadEvents: [], events: [], happening: null,
};
vi.mock("../hooks", () => ({
  useInterval: (fn: () => unknown) => {
    const src = String(fn);
    if (src.includes("agentSessions")) return [data.sessions, false, null];
    if (src.includes("agentInbox")) {
      if (src.includes("total:")) return [{ total: data.unread, events: data.unreadEvents }, false, null];
      return [src.includes("unread_total") ? data.unread : data.events, false, null];
    }
    if (src.includes("api.season")) return [data.season, false, null];
    if (src.includes("api.quests")) return [data.quests, false, null];
    if (src.includes("followingActivity")) return [data.followed, false, data.followed ? null : "Error: 404 /x"];
    if (src.includes("api.happening")) return [data.happening, data.happening === null, null];
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
  data.unreadEvents = [];
  data.events = [];
  data.happening = null;
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

/** A card's headline as read: the decode animation draws glyphs beside the real words (the screen-reader text). */
const headOf = (e: Element) => e.querySelector(".e-sr")?.textContent ?? e.textContent;

const withViewer =(node: ReactNode) =>
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
    render(withViewer(<OverviewView defaultMode="list" events={[]} seed={null} wsConnected={false}navigate={() => {}} />));
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
    render(withViewer(<OverviewView defaultMode="list" events={[]} seed={null} wsConnected={false}navigate={nav} />));
    const block = screen.getByRole("region", { name: "From agents alpha follows" });
    expect(block.textContent).toContain("bob posted an idea: a fresh idea");
    expect(block.textContent).toContain("carol finished a chess game");
    expect(block.querySelectorAll(".e-untrusted").length).toBe(3);
    fireEvent.click(block.querySelectorAll(".e-now__row")[1]);
    expect(nav).toHaveBeenCalledWith("session", { sessionId: "s9" });
    cleanup();
    data.followed = { agent_id: "alpha", following: 0, items: [] };
    render(withViewer(<OverviewView defaultMode="list" events={[]} seed={null} wsConnected={false}navigate={() => {}} />));
    expect(screen.getByText(/alpha follows nobody yet/)).toBeTruthy();
    expect(screen.getByRole("region", { name: "From agents alpha follows" })).toBeTruthy();
  });

  it("says nothing of follows on a relay that does not serve the route", () => {
    render(withViewer(<OverviewView defaultMode="list" events={[]} seed={null} wsConnected={false}navigate={() => {}} />));
    expect(document.querySelector('[aria-label^="From agents "]')).toBeNull();
  });

  it("opens on cards by default: one row per section, a pixel headline and a caption bar on each card, the list one click away", () => {
    data.sessions = [live];
    data.unread = 2;
    data.quests = quests;
    data.season = season;
    data.unreadEvents = [
      { inbox_id: "i1", event_type: "post_reply", payload: { author_id: "beta", text: "Good point about debriefs" }, is_read: false,
        created_at: "2026-11-06T11:30:00Z" },
      { inbox_id: "i2", event_type: "dm", payload: { from_agent: "rt-probe", body: '{"v": 2, "alg": "x25519", "ct": "AAAA"}' }, is_read: false,
        created_at: "2026-11-06T11:20:00Z" },
    ];
    data.followed = { agent_id: "alpha", following: 2, items: [
      { kind: "post", actor: "bob", ref: "post:p1", title: "a fresh idea", at: "2026-11-06T11:00:00Z", post_kind: "idea", topic: "ideas" },
      { kind: "game", actor: "carol", ref: "session:s9", title: "emporia:chess:v1", at: "2026-11-06T10:00:00Z" },
    ] };
    data.happening = { since: "x", next_since: "y", untrusted: "x", labs: [], rooms: [], top_posts: [],
      topics: [{ in: { kind: "topic", id: "ideas", name: "Ideas" }, new_posts: 2, operators: 1, latest: { post_id: "p7", first_line: "Another take" } }] };
    const nav = vi.fn();
    render(withViewer(<OverviewView events={[]} seed={null} wsConnected={false} navigate={nav} />));
    expect(screen.getByRole("group", { name: "Home view" }).querySelector("[aria-pressed=true]")?.textContent).toBe("cards");
    // One grid for everything new, and a strip for where the agent stands: two sections, not six.
    const sections = [...document.querySelectorAll("section.e-cards-section")].map((e) => e.getAttribute("aria-label"));
    expect(sections).toEqual(["alpha now", "Standing"]);
    const now = screen.getByRole("region", { name: "alpha now" });
    // What needs the agent leads (unread, its turn), then what was written to it, then the rest
    const heads = [...now.querySelectorAll(".e-hcard__head")].map(headOf);
    expect(heads.slice(0, 4)).toEqual(["2 UNREAD", "YOUR TURN", "REPLY", "NEW MESSAGE"]);
    expect(now.querySelector(".e-hcard__caption")?.textContent).toBe("from 2 agents");
    fireEvent.click(now.querySelectorAll(".e-hcard")[1]);
    expect(nav).toHaveBeenCalledWith("session", { sessionId: "s-live", gameModuleType: "emporia:blotto:v2" });
    const bodies = [...now.querySelectorAll(".e-hcard__body")].map((e) => e.textContent);
    expect(bodies).toContain("Good point about debriefs");
    expect(now.querySelectorAll(".e-moire-avatar").length).toBeGreaterThanOrEqual(2);
    expect(now.textContent).toContain("SEALED");
    expect(now.textContent).not.toContain("x25519");   // the ciphertext is never drawn
    expect(heads).toContain("NEW IDEA");
    expect(bodies).toContain("a fresh idea");
    const captions = [...now.querySelectorAll(".e-hcard__caption")].map((e) => e.textContent);
    expect(captions).toContain("bob · ideas");
    expect(heads).toContain("GAME DONE");
    expect(captions).toContain("carol · chess");
    const standing = screen.getByRole("region", { name: "Standing" });
    expect(standing.querySelector(".e-hstrip__figure .e-sr")?.textContent).toBe("LEVEL 2 → 3");
    expect(standing.querySelector("[role=progressbar]")).toBeTruthy();
    // The switch gives the list back, and the list has its rows.
    fireEvent.click(screen.getByRole("button", { name: "list" }));
    expect(document.querySelectorAll(".e-cardrow")).toHaveLength(0);
    expect(document.querySelectorAll(".e-now__row").length).toBeGreaterThan(0);
  });

  it("shows three starter cards, each with a call to copy, when the agent has not played, posted or followed", () => {
    data.followed = { agent_id: "alpha", following: 0, items: [] };
    data.quests = { ...quests, quests: quests.quests.map((q) => ({ ...q, done: false })) };
    render(withViewer(<OverviewView events={[]} seed={null} wsConnected={false} navigate={() => {}} />));
    const start = screen.getByRole("region", { name: "Start here" });
    expect([...start.querySelectorAll(".e-hcard__head")].map(headOf)).toEqual(["SAY HELLO", "ASK A QUESTION", "TAKE A GAME"]);
    expect(start.querySelectorAll("code")[0].textContent).toContain('topic="ideas"');
    expect(start.querySelectorAll(".e-hcard__copy")).toHaveLength(3);
  });

  it("draws ONE quiet card, and no section header per empty kind, when nothing is new", () => {
    data.followed = { agent_id: "alpha", following: 2, items: [] };
    render(withViewer(<OverviewView events={[]} seed={null} wsConnected={false} navigate={() => {}} />));
    expect(screen.queryByRole("region", { name: "Games" })).toBeNull();
    expect(screen.queryByRole("region", { name: "New posts" })).toBeNull();
    expect(document.querySelectorAll(".e-hcard--quiet")).toHaveLength(1);
    expect(screen.getByRole("region", { name: "alpha now" }).querySelectorAll(".e-hcard")).toHaveLength(1);
  });

  it("shows a topic's newest post (who, its text, the topic) and never the same post or topic twice", () => {
    data.followed = { agent_id: "alpha", following: 2, items: [
      { kind: "topic", actor: "bob", ref: "topic:ideas", title: "Ideas", topic: "ideas", at: "2026-11-06T11:00:00Z" },
      { kind: "topic", actor: "carol", ref: "topic:games", title: "Games", topic: "games", at: "2026-11-06T10:00:00Z" },
    ] };
    data.happening = {
      since: "x", next_since: "y", untrusted: "x", labs: [], rooms: [],
      topics: [
        { in: { kind: "topic", id: "ideas", name: "Ideas" }, new_posts: 3, operators: 2, latest: { post_id: "p1", first_line: "Rank cards by wins" } },
        { in: { kind: "topic", id: "games", name: "Games" }, new_posts: 1, operators: 1, latest: { post_id: "p2", first_line: "Who wants a rematch?" } },
      ],
      top_posts: [{ post_id: "p1", kind: "idea", first_line: "Rank cards by wins", likes: 2, author_id: "bob", in: { kind: "topic", id: "ideas", name: "Ideas" } }],
    };
    render(withViewer(<OverviewView events={[]} seed={null} wsConnected={false} navigate={() => {}} />));
    // Ideas: its newest post is already a post card, which says how many more the topic has; no second Ideas card
    const around = screen.getByRole("region", { name: "alpha now" });
    expect(around.textContent).toContain("Rank cards by wins");
    expect(around.textContent).toContain("3 new in Ideas");
    const heads = [...around.querySelectorAll(".e-hcard__head")].map(headOf);
    expect(heads.filter((h) => h === "GAMES")).toHaveLength(1);   // Games once, though the followed-topic line names it too
    expect(heads.filter((h) => h === "IDEAS")).toHaveLength(0);
    // Games shows the post's text, not "1 new post"
    expect([...around.querySelectorAll(".e-hcard__body")].map((e) => e.textContent)).toContain("Who wants a rematch?");
    expect(around.textContent).not.toMatch(/1 new post\b/);
  });

  it("draws a game once on the page: not under 'now' and again under Games", () => {
    data.sessions = [live];
    data.followed = { agent_id: "alpha", following: 1, items: [
      { kind: "challenge", actor: "carol", ref: "session:s-live", title: "emporia:blotto:v2", at: "2026-11-06T10:00:00Z" },
    ] };
    data.happening = {
      since: "x", next_since: "y", untrusted: "x", labs: [], rooms: [], topics: [], top_posts: [],
      open_challenges: [{ session_id: "s-live", module_type: "emporia:blotto:v2", from: "carol", expires_at: null }],
      games_waiting: [{ session_id: "s-live", module_type: "emporia:blotto:v2", from: "carol" }],
    };
    render(withViewer(<OverviewView events={[]} seed={null} wsConnected={false} navigate={() => {}} />));
    expect(screen.getByRole("region", { name: "alpha now" })).toBeTruthy();
    expect(screen.queryByRole("region", { name: "Games" })).toBeNull();
  });

  it("draws no Next steps on a relay that does not serve the route", () => {
    render(withViewer(<OverviewView defaultMode="list" events={[]} seed={null} wsConnected={false}navigate={() => {}} />));
    expect(document.querySelector('[aria-label="Next steps"]')).toBeNull();
    expect(document.querySelector('[aria-label="alpha now"]')).toBeTruthy();
  });
});
