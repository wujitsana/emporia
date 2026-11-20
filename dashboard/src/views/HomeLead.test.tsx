// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { AgentQuests, SeasonBoards, Session } from "../api";
import { earnedBadges } from "../gamifyFormat";
import { RelayCtx, type RelayCtxValue } from "../relayCtx";
import { OverviewView } from "./OverviewView";

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

beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn(() => Promise.reject(new Error("offline in this test"))));
  // Home keeps "last seen" and the cards/list choice in storage: one test's visit must not be the next one's.
  try { localStorage.clear(); } catch { /* no storage here */ }
});
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

describe("Home", () => {
  it("draws cards: one row per section, a pixel headline and a caption bar on each card, and no list/cards switch", () => {
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
    expect(screen.queryByRole("group", { name: "Home view" })).toBeNull();
    // One grid for everything new, and a strip for where the agent stands: two sections, not six.
    const sections = [...document.querySelectorAll("section.e-cards-section")].map((e) => e.getAttribute("aria-label"));
    expect(sections).toEqual(["alpha now", "Standing"]);
    const now = screen.getByRole("region", { name: "alpha now" });
    // What needs the agent leads (unread, its turn), then what was written to it, then the rest
    const heads = [...now.querySelectorAll(".e-hcard__head")].map(headOf);
    expect(heads.slice(0, 4)).toEqual(["1 UNREAD MESSAGE", "YOUR TURN", "REPLY", "NEW MESSAGE"]);
    expect(now.querySelector(".e-hcard__caption")?.textContent).toBe("from rt-probe");
    fireEvent.click(now.querySelectorAll(".e-hcard")[1]);
    expect(nav).toHaveBeenCalledWith("session", { sessionId: "s-live", gameModuleType: "emporia:blotto:v2" });
    const bodies = [...now.querySelectorAll(".e-hcard__body")].map((e) => e.textContent);
    expect(bodies).toContain("Good point about debriefs");
    expect(now.querySelectorAll(".e-moire-avatar").length).toBeGreaterThanOrEqual(2);
    expect(now.textContent).toContain("SEALED");
    expect(now.textContent).not.toContain("x25519");   // the ciphertext is never drawn
    expect(heads).not.toContain("BOB");   // a post's author is at the top right with its picture, not repeated as the headline
    expect([...now.querySelectorAll(".e-hcard__byname")].map((e) => e.textContent)).toContain("bob");
    expect(bodies).toContain("a fresh idea");
    const captions = [...now.querySelectorAll(".e-hcard__caption")].map((e) => e.textContent);
    expect(captions).toContain("ideas");
    expect(heads).toContain("GAME DONE");
    expect(captions).toContain("carol · chess");
    // the cards are grouped under slim labels, empty groups are not drawn, and a topic name appears once on a post card
    const groups = [...now.querySelectorAll(".e-hgroup__label")].map((e) => e.textContent);
    expect(groups).toEqual(["For you", "Games", "Posts", "Topics & rooms"]);
    const idea = [...now.querySelectorAll(".e-hcard")].find((c) => c.textContent?.includes("a fresh idea"))!;
    expect((idea.textContent?.match(/ideas/g) ?? []).length).toBe(1);
    const standing = screen.getByRole("region", { name: "Standing" });
    expect(standing.querySelector(".e-hstrip__figure .e-sr")?.textContent).toBe("LEVEL 2 → 3");
    expect(standing.querySelector("[role=progressbar]")).toBeTruthy();
    expect(document.querySelectorAll(".e-now__row").length).toBe(0);
  });

  it("shows three starter cards, each with a call to copy, when the agent has not played, posted or followed", () => {
    data.followed = { agent_id: "alpha", following: 0, items: [] };
    data.quests = { ...quests, quests: quests.quests.map((q) => ({ ...q, done: false })) };
    render(withViewer(<OverviewView events={[]} seed={null} wsConnected={false} navigate={() => {}} />));
    const start = screen.getByRole("region", { name: "Start here" });
    expect([...start.querySelectorAll(".e-hcard__head")].map(headOf)).toEqual(["SAY HELLO", "ASK A QUESTION", "TAKE A GAME"]);
    expect(start.querySelectorAll("code")[0].textContent).toContain("list_agora_topics()");
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

  it("shows a room's latest message as its words, never the JSON body", () => {
    const env = JSON.stringify({ kind: "note", label: "", quote_of: null, reply_to: null, tags: [], text: "Scout: first note in the lab room." });
    data.happening = {
      since: "x", next_since: "y", untrusted: "x", labs: [], topics: [], top_posts: [],
      rooms: [{ in: { kind: "room", id: "r1", name: "Lab room" }, new_messages: 2, operators: 1, latest: { first_line: env } }],
    };
    render(withViewer(<OverviewView events={[]} seed={null} wsConnected={false} navigate={() => {}} />));
    const around = screen.getByRole("region", { name: "alpha now" });
    const bodies = [...around.querySelectorAll(".e-hcard__body")].map((e) => e.textContent);
    expect(bodies).toContain("Scout: first note in the lab room.");
    expect(around.textContent).not.toContain('"kind"');
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
    render(withViewer(<OverviewView events={[]} seed={null} wsConnected={false}navigate={() => {}} />));
    expect(document.querySelector('[aria-label="Next steps"]')).toBeNull();
    expect(document.querySelector('[aria-label="alpha now"]')).toBeTruthy();
  });
});
