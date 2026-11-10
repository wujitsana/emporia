// @vitest-environment jsdom
import { describe, expect, it } from "vitest";
import { hashForView, routeFromHash } from "./dashboardRoute";
import { sessionSection, viewForEvent } from "./eventNav";

const ev = (type: string, extra: Record<string, unknown> = {}) => ({ type, _ts: 1, ...extra });
const r = (view: string, sessionId: string | null = null, lens: string | null = null) => ({ view, sessionId, lens });

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

describe("routes: #/<place>/<section>[/<session>]", () => {
  it("parses a section, a session inside Games or Deals, and an Arena lens", () => {
    expect(routeFromHash("#/arena/games")).toEqual(r("games"));
    expect(routeFromHash("#/arena/games/s-blotto-7f21c")).toEqual(r("games", "s-blotto-7f21c"));
    expect(routeFromHash("#/agent/deals/s-svc-1")).toEqual(r("deals", "s-svc-1"));
    expect(routeFromHash("#/labs/season/alpha")).toEqual(r("season"));
    expect(routeFromHash("#/arena/waiting")).toEqual(r("games", null, "waiting"));
    expect(routeFromHash("#/arena/boards")).toEqual(r("games", null, "boards"));
  });

  it("writes the place first, a lone section as the place, the Topics tab as topics, Money as money", () => {
    expect(hashForView("games", "s-1")).toBe("#/arena/games/s-1");
    expect(hashForView("games", null, null, "results")).toBe("#/arena/results");
    expect(hashForView("games", null, null, "live")).toBe("#/arena");
    expect(hashForView("deals", "s-svc-1")).toBe("#/agent/deals/s-svc-1");
    expect(hashForView("rooms")).toBe("#/agora/rooms");
    expect(hashForView("agora")).toBe("#/agora/topics");
    expect(hashForView("agents")).toBe("#/agora/agents");
    expect(hashForView("messages")).toBe("#/agent/messages");
    expect(hashForView("fees")).toBe("#/agent/money");
    expect(hashForView("overview")).toBe("#/home");
    expect(hashForView("docs")).toBe("#/help");
    expect(hashForView("relay")).toBe("#/desk/relay");
    expect(routeFromHash(hashForView("games", "s-1"))).toEqual(r("games", "s-1"));
  });

  it("opens a bare place on its first section", () => {
    expect(routeFromHash("#/arena")).toEqual(r("games"));
    expect(routeFromHash("#/labs")).toEqual(r("labs"));
    expect(routeFromHash("#/agent")).toEqual(r("profile"));
    expect(routeFromHash("#/agora")).toEqual(r("feed"));
    expect(routeFromHash("#/help")).toEqual(r("docs"));
    expect(routeFromHash("#/desk")).toEqual(r("relay"));
    expect(routeFromHash("#/home")).toEqual(r("overview"));
    expect(routeFromHash("#/help?doc=payments")).toEqual(r("docs"));
    expect(routeFromHash("#/arena/nope")).toEqual(r("games"));
  });

  it("redirects every earlier route to where its content lives now", () => {
    const at = (h: string) => routeFromHash(h);
    expect(at("#/talk")).toEqual(r("feed"));
    expect(at("#/talk/feed").view).toBe("feed");
    expect(at("#/talk/agora").view).toBe("agora");           // the old Agora sub-tab is Topics
    expect(at("#/talk/rooms").view).toBe("rooms");
    expect(at("#/talk/messages").view).toBe("messages");     // now under Agent
    expect(at("#/agents")).toEqual(r("agents"));
    expect(at("#/agents/agents").view).toBe("agents");
    expect(at("#/you")).toEqual(r("profile"));
    expect(at("#/you/stats").view).toBe("stats");
    expect(at("#/you/fees").view).toBe("fees");
    expect(at("#/you/deals").view).toBe("fees");             // Deals folded into Money
    expect(at("#/you/deals/s-svc-1")).toEqual(r("deals", "s-svc-1"));
    expect(at("#/labs/badges").view).toBe("season");         // the catalogue sits in the Season
    expect(at("#/more")).toEqual(r("docs"));
    expect(at("#/more/docs").view).toBe("docs");
    expect(at("#/more/relay").view).toBe("relay");
    expect(at("#/more/operator").view).toBe("operator");
    expect(at("#/more/badges").view).toBe("season");
    expect(at("#/play")).toEqual(r("games"));
    expect(at("#/play/games/s-blotto-7f21c")).toEqual(r("games", "s-blotto-7f21c"));
    expect(at("#/market/deals/s-svc-1")).toEqual(r("deals", "s-svc-1"));
    expect(at("#/market")).toEqual(r("rooms"));
    expect(at("#/market/offers")).toEqual(r("rooms"));
    expect(at("#/market/fees").view).toBe("fees");
    expect(at("#/market/stats").view).toBe("stats");
    expect(at("#/season")).toEqual(r("season"));
    expect(at("#/season/alpha")).toEqual(r("season"));
  });

  it("sends an unknown or retired route to Home", () => {
    for (const h of ["", "#/", "#/sessions", "#/gamify", "#/events", "#/agoras", "#/nope", "#/overview", "#/games/s-9", "#/listings"])
      expect(routeFromHash(h)).toEqual(r("overview"));
  });

  it("refuses a session id that is not an id", () => {
    expect(routeFromHash("#/arena/games/<script>").sessionId).toBeNull();
  });
});
