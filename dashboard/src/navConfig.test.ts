import { describe, expect, it } from "vitest";
import {
  DESTINATIONS, destinationOf, NAV, navForHotkey, siblingView, visibleDestinations, visibleNav,
} from "./navConfig";

// Navigation v2 (docs/ui-nav-v2.md): six destinations, each section in exactly one.
describe("the destinations", () => {
  it("are six, in order, keyed 1–6", () => {
    expect(DESTINATIONS.map((d) => d.label)).toEqual(["Home", "Play", "Market", "Talk", "Season", "More"]);
    expect(DESTINATIONS.map((d) => d.hotkey)).toEqual(["1", "2", "3", "4", "5", "6"]);
  });

  it("place every section in exactly one destination", () => {
    const placed = DESTINATIONS.flatMap((d) => d.views);
    expect(new Set(placed).size).toBe(placed.length);
    expect([...placed].sort()).toEqual(NAV.map((n) => n.id).sort());
    for (const n of NAV) expect(destinationOf(n.id).views).toContain(n.id);
  });

  it("draw each visible section once, and only those", () => {
    for (const opts of [{ isSpectator: true }, { isSpectator: false }, { isSpectator: false, isOperator: true }]) {
      const drawn = visibleDestinations(opts).flatMap((d) => d.items.map((n) => n.id));
      expect([...drawn].sort()).toEqual(visibleNav(opts).map((n) => n.id).sort());
    }
  });

  it("show a first-time viewer at most six places, Home first", () => {
    const dests = visibleDestinations({ isSpectator: true });
    expect(dests.length).toBeLessThanOrEqual(6);
    expect(dests[0].items.map((n) => n.id)).toEqual(["overview"]);
    expect(dests.find((d) => d.id === "play")?.items.map((n) => n.id)).toEqual(["games", "tournaments"]);
    expect(dests.find((d) => d.id === "talk")?.items.map((n) => n.id)).toEqual(["feed", "agora", "rooms"]);
  });

  it("give every section and destination a plain-words line", () => {
    for (const n of NAV) expect(n.blurb.length).toBeGreaterThan(20);
    for (const d of DESTINATIONS) expect(d.blurb.length).toBeGreaterThan(10);
  });
});

// P35-2: the nav and the keys once filtered separately, so the sections the nav
// hides from a spectator stayed one keypress away. Both go through visibleNav.
describe("visibleNav", () => {
  it("hides fees and messages from a spectator", () => {
    const ids = visibleNav({ isSpectator: true }).map((n) => n.id);
    expect(ids).not.toContain("fees");
    expect(ids).not.toContain("messages");
  });

  it("shows every section to an identified operator, and the relay only to one", () => {
    expect(visibleNav({ isSpectator: false, isOperator: true })).toHaveLength(NAV.length);
    expect(visibleNav({ isSpectator: false }).map((n) => n.id)).not.toContain("relay");
  });
});

describe("navForHotkey", () => {
  it("opens a destination's first section on its digit, or the one last seen there", () => {
    for (const spec of [true, false]) {
      expect(navForHotkey("1", { isSpectator: spec })?.id).toBe("overview");
      expect(navForHotkey("2", { isSpectator: spec })?.id).toBe("games");
      expect(navForHotkey("2", { isSpectator: spec }, { play: "tournaments" })?.id).toBe("tournaments");
      expect(navForHotkey("6", { isSpectator: spec })?.id).toBe("agents");
    }
  });

  it("refuses a spectator the keys for sections the nav hides, even as a destination's last", () => {
    expect(navForHotkey("f", { isSpectator: true })).toBeUndefined();
    expect(navForHotkey("4", { isSpectator: true }, { talk: "messages" })?.id).toBe("feed");
    expect(navForHotkey("f", { isSpectator: false })?.id).toBe("fees");
    expect(navForHotkey(".", { isSpectator: false })).toBeUndefined();
    expect(navForHotkey(".", { isSpectator: false, isOperator: true })?.id).toBe("relay");
  });

  it("never claims s, t or T (the tint and theme keys; s is kept free)", () => {
    for (const k of ["s", "t", "T"]) {
      expect(navForHotkey(k, { isSpectator: false, isOperator: true })).toBeUndefined();
    }
    const keys = [...DESTINATIONS.map((d) => d.hotkey), ...NAV.flatMap((n) => (n.hotkey ? [n.hotkey] : []))];
    expect(new Set(keys).size).toBe(keys.length);
  });

  it("reaches every visible section by a key or a destination's strip -- and nothing hidden", () => {
    for (const isSpectator of [true, false]) {
      const shown = new Set(visibleNav({ isSpectator }).map((n) => n.id));
      for (const d of visibleDestinations({ isSpectator })) {
        for (const n of d.items) {
          expect(shown.has(n.id)).toBe(true);
          expect(navForHotkey(d.hotkey, { isSpectator }, { [d.id]: n.id })?.id).toBe(n.id);
        }
      }
    }
  });
});

describe("siblingView ([ and ])", () => {
  it("steps through a destination's visible sections and wraps", () => {
    const viewer = { isSpectator: false };
    expect(siblingView("listings", 1, viewer)).toBe("deals");
    expect(siblingView("stats", 1, viewer)).toBe("listings");
    expect(siblingView("listings", -1, viewer)).toBe("stats");
    expect(siblingView("deals", 1, { isSpectator: true })).toBe("listings");
    expect(siblingView("overview", 1, viewer)).toBe("overview");
  });
});

describe("the stats section (Market › Stats)", () => {
  it("is the agent's own: in Market after Fees, at #/market/stats, never a spectator's", async () => {
    const { hashForView, routeFromHash } = await import("./dashboardRoute");
    const viewer = { isSpectator: false };
    const market = visibleDestinations(viewer).find((d) => d.id === "market")!.items.map((n) => n.id);
    expect(market).toEqual(["listings", "deals", "fees", "stats"]);
    expect(hashForView("stats")).toBe("#/market/stats");
    expect(routeFromHash("#/market/stats").view).toBe("stats");
    expect(routeFromHash("#/stats").view).toBe("stats");
    expect(siblingView("fees", 1, viewer)).toBe("stats");
    expect(navForHotkey("3", viewer, { market: "stats" })?.id).toBe("stats");
    expect(visibleNav({ isSpectator: true }).map((n) => n.id)).not.toContain("stats");
    expect(navForHotkey("3", { isSpectator: true }, { market: "stats" })?.id).toBe("listings");
  });
});

describe("the operator section", () => {
  it("is the relay operator's alone, by the nav, by its key and in More after Relay", () => {
    for (const isSpectator of [true, false]) {
      expect(visibleNav({ isSpectator }).map((n) => n.id)).not.toContain("operator");
      expect(navForHotkey("o", { isSpectator })).toBeUndefined();
      expect(visibleDestinations({ isSpectator }).flatMap((d) => d.items.map((n) => n.id))).not.toContain("operator");
    }
    const op = { isSpectator: false, isOperator: true };
    expect(visibleNav(op).map((n) => n.id)).toContain("operator");
    expect(navForHotkey("o", op)?.id).toBe("operator");
    const more = visibleDestinations(op).find((d) => d.id === "more")!.items.map((n) => n.id);
    expect(more.indexOf("operator")).toBe(more.indexOf("relay") + 1);
  });
});
