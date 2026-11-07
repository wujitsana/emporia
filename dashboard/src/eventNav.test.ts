// @vitest-environment jsdom
import { describe, expect, it } from "vitest";
import { hashForView, routeFromHash } from "./dashboardRoute";
import { sessionSection, viewForEvent } from "./eventNav";

const ev = (type: string, extra: Record<string, unknown> = {}) => ({ type, _ts: 1, ...extra });

describe("a session opens in Games or Deals by what it is", () => {
  it("routes set plays and chess to Games, jobs to Deals", () => {
    expect(sessionSection("emporia:blotto:v2")).toBe("games");
    expect(sessionSection("emporia:chess:v1")).toBe("games");
    expect(sessionSection("emporia:service:v1")).toBe("deals");
    expect(sessionSection("emporia:code-review:v1")).toBe("deals");
    expect(sessionSection("emporia:research:v1")).toBe("deals");
    expect(sessionSection(undefined)).toBe("games");
  });
});

describe("live feed event → where it opens", () => {
  it("opens a session wherever it lives, carrying its module when the event has one", () => {
    expect(viewForEvent(ev("session_created", { session_id: "s1", module_type: "emporia:blotto:v2" })))
      .toEqual({ view: "session", opts: { sessionId: "s1", gameModuleType: "emporia:blotto:v2" } });
    expect(viewForEvent(ev("action_result", { session_id: "s2" }))?.view).toBe("session");
    expect(viewForEvent(ev("your_turn", { session_id: "s3" }))?.view).toBe("session");
  });

  it("maps the rest to the new sections", () => {
    expect(viewForEvent(ev("room_message", { room_id: "r1" }))).toEqual({ view: "rooms", opts: { roomId: "r1" } });
    expect(viewForEvent(ev("room_invite", { room_id: "r1" }))?.view).toBe("messages");
    expect(viewForEvent(ev("listing_created", { listing_id: "l1" }))?.view).toBe("listings");
    expect(viewForEvent(ev("agora_post_created"))?.view).toBe("agora");
    expect(viewForEvent(ev("agora_invite"))?.view).toBe("messages");
    expect(viewForEvent(ev("dm_received"))?.view).toBe("messages");
    expect(viewForEvent(ev("challenge"))?.view).toBe("messages");
    expect(viewForEvent(ev("like_added", { agent_id: "a" }))?.view).toBe("season");
    expect(viewForEvent(ev("agent_registered", { agent_id: "a" }))?.view).toBe("agents");
    expect(viewForEvent(ev("settlement_recorded"))?.view).toBe("overview");
    expect(viewForEvent(ev("pong"))).toBeNull();
  });
});

describe("routes", () => {
  it("parses a section, and a session inside Games or Deals", () => {
    expect(routeFromHash("#/games")).toEqual({ view: "games", sessionId: null });
    expect(routeFromHash("#/games/s-blotto-7f21c")).toEqual({ view: "games", sessionId: "s-blotto-7f21c" });
    expect(routeFromHash("#/deals/s-svc-1")).toEqual({ view: "deals", sessionId: "s-svc-1" });
    expect(routeFromHash("#/season/alpha")).toEqual({ view: "season", sessionId: null });
  });

  it("sends an unknown or retired route to the Overview", () => {
    for (const h of ["", "#/", "#/sessions", "#/gamify", "#/events", "#/agoras", "#/nope"])
      expect(routeFromHash(h)).toEqual({ view: "overview", sessionId: null });
  });

  it("refuses a session id that is not an id", () => {
    expect(routeFromHash("#/games/<script>").sessionId).toBeNull();
  });

  it("round-trips", () => {
    expect(routeFromHash(hashForView("games", "s-1"))).toEqual({ view: "games", sessionId: "s-1" });
    expect(hashForView("season", "s-1")).toBe("#/season");
  });
});

describe("routes v2: #/<destination>/<section>[/<session>]", () => {
  it("writes the destination first, and a lone section as the destination", () => {
    expect(hashForView("games", "s-1")).toBe("#/play/games/s-1");
    expect(hashForView("deals", "s-svc-1")).toBe("#/market/deals/s-svc-1");
    expect(hashForView("rooms")).toBe("#/talk/rooms");
    expect(hashForView("overview")).toBe("#/home");
    expect(hashForView("docs")).toBe("#/more/docs");
  });

  it("parses them, and a bare destination opens its first section", () => {
    expect(routeFromHash("#/play/games/s-1")).toEqual({ view: "games", sessionId: "s-1" });
    expect(routeFromHash("#/play")).toEqual({ view: "games", sessionId: null });
    expect(routeFromHash("#/market")).toEqual({ view: "listings", sessionId: null });
    expect(routeFromHash("#/talk/messages")).toEqual({ view: "messages", sessionId: null });
    expect(routeFromHash("#/more")).toEqual({ view: "agents", sessionId: null });
    expect(routeFromHash("#/home")).toEqual({ view: "overview", sessionId: null });
    expect(routeFromHash("#/more/docs?doc=payments")).toEqual({ view: "docs", sessionId: null });
    expect(routeFromHash("#/play/nope")).toEqual({ view: "games", sessionId: null });
  });

  it("keeps every v1 route opening what it opened", () => {
    const v1: [string, string, string | null][] = [
      ["#/overview", "overview", null], ["#/games/s-9", "games", "s-9"], ["#/deals/s-8", "deals", "s-8"],
      ["#/season", "season", null], ["#/listings", "listings", null],
      ["#/fees", "fees", null], ["#/agora", "agora", null], ["#/rooms", "rooms", null],
      ["#/messages", "messages", null], ["#/agents", "agents", null], ["#/profile", "profile", null],
      ["#/settings", "settings", null], ["#/relay", "relay", null], ["#/docs?doc=games", "docs", null],
    ];
    for (const [h, view, sessionId] of v1) expect(routeFromHash(h)).toEqual({ view, sessionId });
  });
});
