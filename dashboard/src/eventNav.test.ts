// @vitest-environment jsdom
import { describe, expect, it } from "vitest";
import { hashForView, routeFromHash } from "./dashboardRoute";
import { sessionSection, viewForEvent } from "./eventNav";
import { viewForListing } from "./listingNav";
import type { Listing } from "./api";

const ev = (type: string, extra: Record<string, unknown> = {}) => ({ type, _ts: 1, ...extra });

describe("a session opens in Games or Deals by what it is", () => {
  it("routes set plays and chess to Games, jobs to Deals", () => {
    expect(sessionSection("emporia:blotto:v1")).toBe("games");
    expect(sessionSection("emporia:chess:v1")).toBe("games");
    expect(sessionSection("emporia:service:v1")).toBe("deals");
    expect(sessionSection("emporia:code-review:v1")).toBe("deals");
    expect(sessionSection("emporia:research:v1")).toBe("deals");
    expect(sessionSection(undefined)).toBe("games");
  });

  it("a listing opens where its sessions live", () => {
    const l = (listing_type: string, module_type?: string) => ({ listing_id: "l", listing_type, module_type, title: "t",
      description: "", payment_mode: "free", price_usd: "0", agent_id: "a" }) as Listing;
    expect(viewForListing(l("game", "emporia:two-thirds:v1")).view).toBe("games");
    expect(viewForListing(l("service", "emporia:service:v1")).view).toBe("deals");
    expect(viewForListing(l("event")).view).toBe("tournaments");
    expect(viewForListing(l("room")).view).toBe("rooms");
  });
});

describe("live feed event → where it opens", () => {
  it("opens a session wherever it lives, carrying its module when the event has one", () => {
    expect(viewForEvent(ev("session_created", { session_id: "s1", module_type: "emporia:blotto:v1" })))
      .toEqual({ view: "session", opts: { sessionId: "s1", gameModuleType: "emporia:blotto:v1" } });
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
    expect(viewForEvent(ev("bug_reported"))?.view).toBe("season");
    expect(viewForEvent(ev("agent_registered", { agent_id: "a" }))?.view).toBe("agents");
    expect(viewForEvent(ev("event_created", { event_id: "e1" }))).toEqual({ view: "tournaments", opts: { eventId: "e1" } });
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
