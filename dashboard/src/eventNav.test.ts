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
    expect(routeFromHash("#/arena/games")).toEqual({ view: "games", sessionId: null });
    expect(routeFromHash("#/arena/games/s-blotto-7f21c")).toEqual({ view: "games", sessionId: "s-blotto-7f21c" });
    expect(routeFromHash("#/you/deals/s-svc-1")).toEqual({ view: "deals", sessionId: "s-svc-1" });
    expect(routeFromHash("#/labs/season/alpha")).toEqual({ view: "season", sessionId: null });
  });

  it("still opens every route of the earlier layout, where its content lives now", () => {
    const at = (h: string) => routeFromHash(h);
    expect(at("#/play")).toEqual({ view: "games", sessionId: null });
    expect(at("#/play/games/s-blotto-7f21c")).toEqual({ view: "games", sessionId: "s-blotto-7f21c" });
    expect(at("#/market/deals/s-svc-1")).toEqual({ view: "deals", sessionId: "s-svc-1" });
    expect(at("#/market")).toEqual({ view: "rooms", sessionId: null });
    expect(at("#/market/offers")).toEqual({ view: "rooms", sessionId: null });
    expect(at("#/market/fees").view).toBe("fees");
    expect(at("#/market/stats").view).toBe("stats");
    expect(at("#/season")).toEqual({ view: "season", sessionId: null });
    expect(at("#/season/alpha")).toEqual({ view: "season", sessionId: null });
    expect(at("#/more/agents").view).toBe("agents");
    expect(at("#/more/profile").view).toBe("profile");
    expect(at("#/more/settings").view).toBe("settings");
    expect(at("#/more/badges").view).toBe("badges");
    expect(at("#/more")).toEqual({ view: "docs", sessionId: null });
  });

  it("sends an unknown or retired route to the Overview", () => {
    for (const h of ["", "#/", "#/sessions", "#/gamify", "#/events", "#/agoras", "#/nope"])
      expect(routeFromHash(h)).toEqual({ view: "overview", sessionId: null });
  });

  it("refuses a session id that is not an id", () => {
    expect(routeFromHash("#/arena/games/<script>").sessionId).toBeNull();
  });

  it("round-trips", () => {
    expect(routeFromHash(hashForView("games", "s-1"))).toEqual({ view: "games", sessionId: "s-1" });
    expect(hashForView("season", "s-1")).toBe("#/labs/season");
  });
});

describe("routes v2: #/<destination>/<section>[/<session>]", () => {
  it("writes the destination first, and a lone section as the destination", () => {
    expect(hashForView("games", "s-1")).toBe("#/arena/games/s-1");
    expect(hashForView("deals", "s-svc-1")).toBe("#/you/deals/s-svc-1");
    expect(hashForView("rooms")).toBe("#/talk/rooms");
    expect(hashForView("overview")).toBe("#/home");
    expect(hashForView("docs")).toBe("#/more/docs");
  });

  it("parses them, and a bare destination opens its first section", () => {
    expect(routeFromHash("#/arena/games/s-1")).toEqual({ view: "games", sessionId: "s-1" });
    expect(routeFromHash("#/arena")).toEqual({ view: "games", sessionId: null });
    expect(routeFromHash("#/labs")).toEqual({ view: "labs", sessionId: null });
    expect(routeFromHash("#/you")).toEqual({ view: "profile", sessionId: null });
    expect(routeFromHash("#/talk/messages")).toEqual({ view: "messages", sessionId: null });
    expect(routeFromHash("#/agents")).toEqual({ view: "agents", sessionId: null });
    expect(routeFromHash("#/home")).toEqual({ view: "overview", sessionId: null });
    expect(routeFromHash("#/more/docs?doc=payments")).toEqual({ view: "docs", sessionId: null });
    expect(routeFromHash("#/arena/nope")).toEqual({ view: "games", sessionId: null });
  });

  it("opens Home for a route that names no destination", () => {
    for (const h of ["#/overview", "#/games/s-9", "#/listings", "#/nope"])
      expect(routeFromHash(h)).toEqual({ view: "overview", sessionId: null });
  });
});
