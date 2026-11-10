import { describe, expect, it } from "vitest";
import {
  ARENA_LENSES, dealsAvailable, DESTINATIONS, destinationOf, HEADER_DESTINATIONS, NAV, navForHotkey, siblingView,
  visibleDestinations, visibleHeaderDestinations, visibleNav,
} from "./navConfig";

const ids = (list: { id: string }[]) => list.map((n) => n.id);

// Five places, two header-only ones; each section in exactly one.
describe("the destinations", () => {
  it("are five, in order, keyed 1–5", () => {
    expect(DESTINATIONS.map((d) => d.label)).toEqual(["Home", "Agora", "Arena", "Messages", "Agent"]);
    expect(DESTINATIONS.map((d) => d.hotkey)).toEqual(["1", "2", "3", "4", "5"]);
  });

  it("hold the sections the layout names, and no Market, Offers, Talk or Badges tab", () => {
    const of = (id: string) => DESTINATIONS.find((d) => d.id === id)!.views;
    expect(of("home")).toEqual(["overview"]);
    expect(of("messages")).toEqual(["messages"]);
    expect(of("agora")).toEqual(["feed", "agora", "rooms", "labs", "agents"]);
    expect(of("arena")).toEqual(["games", "season"]);
    expect(of("agent")).toEqual(["profile", "settings", "stats", "fees", "deals"]);
    expect(HEADER_DESTINATIONS.map((d) => d.views)).toEqual([["docs"], ["relay", "operator"]]);
    for (const gone of ["market", "talk", "you", "more", "agents"])
      expect(DESTINATIONS.map((d) => d.id as string)).not.toContain(gone);
    expect(NAV.map((n) => n.id as string)).not.toContain("offers");
    expect(NAV.map((n) => n.id as string)).not.toContain("badges");
  });

  it("call the old Agora tab Topics, and keep the section id", () => {
    expect(NAV.find((n) => n.id === "agora")?.label).toBe("Topics");
  });

  it("place every section in exactly one destination", () => {
    const placed = [...DESTINATIONS, ...HEADER_DESTINATIONS].flatMap((d) => d.views);
    expect(new Set(placed).size).toBe(placed.length);
    expect([...placed].sort()).toEqual(NAV.map((n) => n.id).sort());
    for (const n of NAV) expect(destinationOf(n.id).views).toContain(n.id);
  });

  it("draw each visible section once, and only those", () => {
    for (const dealsOn of [false, true]) {
      for (const opts of [{ isSpectator: true }, { isSpectator: false }, { isSpectator: false, isOperator: true }]) {
        const o = { ...opts, dealsOn };
        const drawn = [...visibleDestinations(o), ...visibleHeaderDestinations(o)].flatMap((d) => d.items.map((n) => n.id));
        expect([...drawn].sort()).toEqual(visibleNav(o).map((n) => n.id).sort());
      }
    }
  });

  it("show a first-time viewer five places, Home first", () => {
    const dests = visibleDestinations({ isSpectator: true });
    expect(dests.length).toBe(5);
    expect(dests[0].items.map((n) => n.id)).toEqual(["overview"]);
    expect(ids(dests.map((d) => ({ id: d.id })))).toEqual(["home", "agora", "arena", "messages", "agent"]);
    expect(ids(dests.find((d) => d.id === "agora")!.items)).toEqual(["feed", "agora", "rooms", "labs", "agents"]);
    expect(ids(dests.find((d) => d.id === "arena")!.items)).toEqual(["games", "season"]);
    expect(ids(dests.find((d) => d.id === "agent")!.items)).toEqual(["profile", "settings"]);
  });

  it("give every section and destination a plain-words line", () => {
    for (const n of NAV) expect(n.blurb.length).toBeGreaterThan(20);
    for (const d of [...DESTINATIONS, ...HEADER_DESTINATIONS]) expect(d.blurb.length).toBeGreaterThan(10);
  });

  it("give the Arena four lenses", () => {
    expect(ARENA_LENSES.map((l) => l.label)).toEqual(["Live", "Waiting", "Results", "Boards"]);
  });

  it("draw the header's Help always and the relay desk for the operator alone", () => {
    expect(ids(visibleHeaderDestinations({ isSpectator: true }))).toEqual(["help"]);
    expect(ids(visibleHeaderDestinations({ isSpectator: false }))).toEqual(["help"]);
    expect(ids(visibleHeaderDestinations({ isSpectator: false, isOperator: true }))).toEqual(["help", "desk"]);
  });
});

// Deals is drawn only on a relay that can sell services, and last, so nothing shifts when it appears.
describe("Deals gating", () => {
  const agent = (o: { isSpectator: boolean; dealsOn?: boolean }) =>
    visibleDestinations(o).find((d) => d.id === "agent")!.items.map((n) => n.id);

  it("reads /health: escrow on, the money module's escrow part, or cards live", () => {
    expect(dealsAvailable(null)).toBe(false);
    expect(dealsAvailable({})).toBe(false);
    expect(dealsAvailable({ x402_escrow_network: null, stripe_enabled: false })).toBe(false);
    expect(dealsAvailable({ x402_escrow_network: "eip155:84532" })).toBe(true);
    expect(dealsAvailable({ relay_modules: { money: { parts: ["x402", "escrow"] } } })).toBe(true);
    expect(dealsAvailable({ relay_modules: { money: { parts: ["x402"] } } })).toBe(false);
    // Cards alone are not deals: with escrow off (this release) the tab is not drawn.
    expect(dealsAvailable({ stripe_enabled: true, stripe_mode: "test" })).toBe(false);
    expect(dealsAvailable({ stripe_enabled: true, stripe_mode: "live" })).toBe(false);
    expect(dealsAvailable({ relay_modules: { money: { parts: ["cards", "x402"] } } })).toBe(false);
  });

  it("puts Deals last in Agent, only when the relay can sell", () => {
    expect(agent({ isSpectator: false })).toEqual(["profile", "settings", "stats", "fees"]);
    expect(agent({ isSpectator: false, dealsOn: true })).toEqual(["profile", "settings", "stats", "fees", "deals"]);
    expect(agent({ isSpectator: true, dealsOn: true })).toEqual(["profile", "settings", "deals"]);
  });

  it("takes it out of the keys and the [ ] strip as well as the nav", () => {
    const off = { isSpectator: false };
    const on = { isSpectator: false, dealsOn: true };
    expect(visibleNav(off).map((n) => n.id)).not.toContain("deals");
    expect(navForHotkey("5", off, { agent: "deals" })?.id).toBe("profile");
    expect(navForHotkey("5", on, { agent: "deals" })?.id).toBe("deals");
    expect(siblingView("fees", 1, off)).toBe("profile");
    expect(siblingView("fees", 1, on)).toBe("deals");
  });
});

// P35-2: the nav and the keys once filtered separately, so the sections the nav
// hides from a spectator stayed one keypress away. Both go through visibleNav.
describe("visibleNav", () => {
  it("hides money and stats from a spectator, but keeps Messages (its page says to pick an agent)", () => {
    const names = visibleNav({ isSpectator: true }).map((n) => n.id);
    expect(names).not.toContain("fees");
    expect(names).toContain("messages");
    expect(names).not.toContain("stats");
  });

  it("shows every section to an identified operator on a relay that sells, and the relay only to one", () => {
    expect(visibleNav({ isSpectator: false, isOperator: true, dealsOn: true })).toHaveLength(NAV.length);
    expect(visibleNav({ isSpectator: false }).map((n) => n.id)).not.toContain("relay");
  });
});

describe("navForHotkey", () => {
  it("opens a destination's first section on its digit, or the one last seen there", () => {
    for (const spec of [true, false]) {
      expect(navForHotkey("1", { isSpectator: spec })?.id).toBe("overview");
      expect(navForHotkey("2", { isSpectator: spec })?.id).toBe("feed");
      expect(navForHotkey("3", { isSpectator: spec })?.id).toBe("games");
      expect(navForHotkey("4", { isSpectator: spec })?.id).toBe("messages");
      expect(navForHotkey("5", { isSpectator: spec })?.id).toBe("profile");
      expect(navForHotkey("3", { isSpectator: spec }, { arena: "season" })?.id).toBe("season");
    }
    expect(navForHotkey("6", { isSpectator: false })).toBeUndefined();
    expect(navForHotkey("?", { isSpectator: false })?.id).toBe("docs");
  });

  it("refuses a spectator the keys for sections the nav hides, even as a destination's last", () => {
    expect(navForHotkey("f", { isSpectator: true })).toBeUndefined();
    expect(navForHotkey("5", { isSpectator: true }, { agent: "stats" })?.id).toBe("profile");
    expect(navForHotkey("f", { isSpectator: false })?.id).toBe("fees");
    expect(navForHotkey(".", { isSpectator: false })).toBeUndefined();
    expect(navForHotkey(".", { isSpectator: false, isOperator: true })?.id).toBe("relay");
    expect(navForHotkey("o", { isSpectator: false })).toBeUndefined();
    expect(navForHotkey("o", { isSpectator: false, isOperator: true })?.id).toBe("operator");
  });

  it("never claims s, t or T (the tint and theme keys; s is kept free)", () => {
    for (const k of ["s", "t", "T"]) {
      expect(navForHotkey(k, { isSpectator: false, isOperator: true, dealsOn: true })).toBeUndefined();
    }
    const keys = [...DESTINATIONS.map((d) => d.hotkey), ...NAV.flatMap((n) => (n.hotkey ? [n.hotkey] : []))];
    expect(new Set(keys).size).toBe(keys.length);
  });

  it("reaches every visible section of a destination by its digit and the last-seen memory", () => {
    for (const isSpectator of [true, false]) {
      for (const dealsOn of [false, true]) {
        const o = { isSpectator, dealsOn };
        for (const d of visibleDestinations(o)) {
          for (const n of d.items) expect(navForHotkey(d.hotkey, o, { [d.id]: n.id })?.id).toBe(n.id);
        }
      }
    }
  });
});

describe("siblingView ([ and ])", () => {
  it("steps through a destination's visible sections and wraps", () => {
    const viewer = { isSpectator: false };
    expect(siblingView("feed", 1, viewer)).toBe("agora");
    expect(siblingView("agents", 1, viewer)).toBe("feed");
    expect(siblingView("feed", -1, viewer)).toBe("agents");
    expect(siblingView("rooms", 1, viewer)).toBe("labs");
    expect(siblingView("games", 1, viewer)).toBe("season");
    expect(siblingView("season", 1, viewer)).toBe("games");
    expect(siblingView("overview", 1, viewer)).toBe("overview");
    expect(siblingView("messages", 1, viewer)).toBe("messages");
    expect(siblingView("relay", 1, { isSpectator: false, isOperator: true })).toBe("operator");
  });
});

describe("the stats section (Agent › Stats)", () => {
  it("is the agent's own: in Agent after Settings, at #/agent/stats, never a spectator's", async () => {
    const { hashForView, routeFromHash } = await import("./dashboardRoute");
    const viewer = { isSpectator: false };
    const agent = visibleDestinations(viewer).find((d) => d.id === "agent")!.items.map((n) => n.id);
    expect(agent).toEqual(["profile", "settings", "stats", "fees"]);
    expect(hashForView("stats")).toBe("#/agent/stats");
    expect(routeFromHash("#/agent/stats").view).toBe("stats");
    // Routes from before Messages and Season moved still open where they live now.
    expect(routeFromHash("#/agent/messages").view).toBe("messages");
    expect(routeFromHash("#/labs/season").view).toBe("season");
    expect(routeFromHash("#/labs/labs").view).toBe("labs");
    expect(routeFromHash("#/labs").view).toBe("labs");
    expect(hashForView("messages")).toBe("#/messages");
    expect(hashForView("labs")).toBe("#/agora/labs");
    expect(hashForView("season")).toBe("#/arena/season");
    expect(routeFromHash("#/you/stats").view).toBe("stats");
    expect(routeFromHash("#/market/stats").view).toBe("stats");
    expect(navForHotkey("5", viewer, { agent: "stats" })?.id).toBe("stats");
    expect(visibleNav({ isSpectator: true }).map((n) => n.id)).not.toContain("stats");
  });
});

describe("the operator section", () => {
  it("is the relay operator's alone, by the nav, by its key and in the header's relay desk after Relay", () => {
    for (const isSpectator of [true, false]) {
      expect(visibleNav({ isSpectator }).map((n) => n.id)).not.toContain("operator");
      expect(navForHotkey("o", { isSpectator })).toBeUndefined();
    }
    const op = { isSpectator: false, isOperator: true };
    expect(visibleNav(op).map((n) => n.id)).toContain("operator");
    expect(navForHotkey("o", op)?.id).toBe("operator");
    const desk = visibleHeaderDestinations(op).find((d) => d.id === "desk")!.items.map((n) => n.id);
    expect(desk).toEqual(["relay", "operator"]);
  });
});
