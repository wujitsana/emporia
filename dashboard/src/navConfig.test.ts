import { describe, expect, it } from "vitest";
import {
  dealsAvailable, DESTINATIONS, destinationOf, NAV, navForHotkey, siblingView, visibleDestinations, visibleNav,
} from "./navConfig";

// Seven destinations, each section in exactly one.
describe("the destinations", () => {
  it("are seven, in order, keyed 1–7", () => {
    expect(DESTINATIONS.map((d) => d.label)).toEqual(["Home", "Talk", "Arena", "Labs & Season", "Agents", "You", "More"]);
    expect(DESTINATIONS.map((d) => d.hotkey)).toEqual(["1", "2", "3", "4", "5", "6", "7"]);
  });

  it("hold the sections the layout names, and no Market or Offers", () => {
    const of = (id: string) => DESTINATIONS.find((d) => d.id === id)!.views;
    expect(of("home")).toEqual(["overview"]);
    expect(of("talk")).toEqual(["feed", "agora", "rooms", "messages"]);
    expect(of("arena")).toEqual(["games"]);
    expect(of("labs")).toEqual(["labs", "season", "badges"]);
    expect(of("agents")).toEqual(["agents"]);
    expect(of("you")).toEqual(["profile", "stats", "fees", "deals", "settings"]);
    expect(of("more")).toEqual(["relay", "operator", "docs"]);
    expect(DESTINATIONS.map((d) => d.id as string)).not.toContain("market");
    expect(NAV.map((n) => n.id as string)).not.toContain("offers");
  });

  it("place every section in exactly one destination", () => {
    const placed = DESTINATIONS.flatMap((d) => d.views);
    expect(new Set(placed).size).toBe(placed.length);
    expect([...placed].sort()).toEqual(NAV.map((n) => n.id).sort());
    for (const n of NAV) expect(destinationOf(n.id).views).toContain(n.id);
  });

  it("draw each visible section once, and only those", () => {
    for (const dealsOn of [false, true]) {
      for (const opts of [{ isSpectator: true }, { isSpectator: false }, { isSpectator: false, isOperator: true }]) {
        const o = { ...opts, dealsOn };
        const drawn = visibleDestinations(o).flatMap((d) => d.items.map((n) => n.id));
        expect([...drawn].sort()).toEqual(visibleNav(o).map((n) => n.id).sort());
      }
    }
  });

  it("show a first-time viewer at most seven places, Home first", () => {
    const dests = visibleDestinations({ isSpectator: true });
    expect(dests.length).toBeLessThanOrEqual(7);
    expect(dests[0].items.map((n) => n.id)).toEqual(["overview"]);
    expect(dests.find((d) => d.id === "arena")?.items.map((n) => n.id)).toEqual(["games"]);
    expect(dests.find((d) => d.id === "talk")?.items.map((n) => n.id)).toEqual(["feed", "agora", "rooms"]);
    expect(dests.find((d) => d.id === "labs")?.items.map((n) => n.id)).toEqual(["labs", "season", "badges"]);
    expect(dests.find((d) => d.id === "you")?.items.map((n) => n.id)).toEqual(["profile", "settings"]);
  });

  it("give every section and destination a plain-words line", () => {
    for (const n of NAV) expect(n.blurb.length).toBeGreaterThan(20);
    for (const d of DESTINATIONS) expect(d.blurb.length).toBeGreaterThan(10);
  });
});

// Deals is drawn only on a relay that can sell services.
describe("Deals gating", () => {
  const you = (o: { isSpectator: boolean; dealsOn?: boolean }) =>
    visibleDestinations(o).find((d) => d.id === "you")!.items.map((n) => n.id);

  it("reads /health: escrow on, the money module's escrow part, or cards live", () => {
    expect(dealsAvailable(null)).toBe(false);
    expect(dealsAvailable({})).toBe(false);
    expect(dealsAvailable({ x402_escrow_network: null, stripe_enabled: false })).toBe(false);
    expect(dealsAvailable({ x402_escrow_network: "eip155:84532" })).toBe(true);
    expect(dealsAvailable({ relay_modules: { money: { parts: ["x402", "escrow"] } } })).toBe(true);
    expect(dealsAvailable({ relay_modules: { money: { parts: ["x402"] } } })).toBe(false);
    expect(dealsAvailable({ stripe_enabled: true, stripe_mode: "test" })).toBe(true);
    expect(dealsAvailable({ stripe_enabled: true, stripe_mode: "live" })).toBe(true);
    expect(dealsAvailable({ stripe_enabled: true, stripe_mode: "" })).toBe(false);
    expect(dealsAvailable({ stripe_enabled: false, stripe_mode: "live" })).toBe(false);
  });

  it("puts Deals under You, between Money and Settings, only when the relay can sell", () => {
    expect(you({ isSpectator: false })).toEqual(["profile", "stats", "fees", "settings"]);
    expect(you({ isSpectator: false, dealsOn: true })).toEqual(["profile", "stats", "fees", "deals", "settings"]);
    expect(you({ isSpectator: true, dealsOn: true })).toEqual(["profile", "deals", "settings"]);
  });

  it("takes it out of the keys and the [ ] strip as well as the nav", () => {
    const off = { isSpectator: false };
    const on = { isSpectator: false, dealsOn: true };
    expect(visibleNav(off).map((n) => n.id)).not.toContain("deals");
    expect(navForHotkey("6", off, { you: "deals" })?.id).toBe("profile");
    expect(navForHotkey("6", on, { you: "deals" })?.id).toBe("deals");
    expect(siblingView("fees", 1, off)).toBe("settings");
    expect(siblingView("fees", 1, on)).toBe("deals");
  });
});

// P35-2: the nav and the keys once filtered separately, so the sections the nav
// hides from a spectator stayed one keypress away. Both go through visibleNav.
describe("visibleNav", () => {
  it("hides money and messages from a spectator", () => {
    const ids = visibleNav({ isSpectator: true }).map((n) => n.id);
    expect(ids).not.toContain("fees");
    expect(ids).not.toContain("messages");
    expect(ids).not.toContain("stats");
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
      expect(navForHotkey("3", { isSpectator: spec })?.id).toBe("games");
      expect(navForHotkey("4", { isSpectator: spec })?.id).toBe("labs");
      expect(navForHotkey("5", { isSpectator: spec })?.id).toBe("agents");
      expect(navForHotkey("6", { isSpectator: spec })?.id).toBe("profile");
      expect(navForHotkey("4", { isSpectator: spec }, { labs: "season" })?.id).toBe("season");
    }
    expect(navForHotkey("2", { isSpectator: false })?.id).toBe("feed");
    expect(navForHotkey("7", { isSpectator: false })?.id).toBe("docs");
    expect(navForHotkey("7", { isSpectator: false, isOperator: true })?.id).toBe("relay");
    expect(navForHotkey("8", { isSpectator: false, isOperator: true })).toBeUndefined();
  });

  it("refuses a spectator the keys for sections the nav hides, even as a destination's last", () => {
    expect(navForHotkey("f", { isSpectator: true })).toBeUndefined();
    expect(navForHotkey("2", { isSpectator: true }, { talk: "messages" })?.id).toBe("feed");
    expect(navForHotkey("f", { isSpectator: false })?.id).toBe("fees");
    expect(navForHotkey(".", { isSpectator: false })).toBeUndefined();
    expect(navForHotkey(".", { isSpectator: false, isOperator: true })?.id).toBe("relay");
  });

  it("never claims s, t or T (the tint and theme keys; s is kept free)", () => {
    for (const k of ["s", "t", "T"]) {
      expect(navForHotkey(k, { isSpectator: false, isOperator: true, dealsOn: true })).toBeUndefined();
    }
    const keys = [...DESTINATIONS.map((d) => d.hotkey), ...NAV.flatMap((n) => (n.hotkey ? [n.hotkey] : []))];
    expect(new Set(keys).size).toBe(keys.length);
  });

  it("reaches every visible section by a key or a destination's strip -- and nothing hidden", () => {
    for (const isSpectator of [true, false]) {
      for (const dealsOn of [false, true]) {
        const o = { isSpectator, dealsOn };
        const shown = new Set(visibleNav(o).map((n) => n.id));
        for (const d of visibleDestinations(o)) {
          for (const n of d.items) {
            expect(shown.has(n.id)).toBe(true);
            expect(navForHotkey(d.hotkey, o, { [d.id]: n.id })?.id).toBe(n.id);
          }
        }
      }
    }
  });
});

describe("siblingView ([ and ])", () => {
  it("steps through a destination's visible sections and wraps", () => {
    const viewer = { isSpectator: false };
    expect(siblingView("feed", 1, viewer)).toBe("agora");
    expect(siblingView("messages", 1, viewer)).toBe("feed");
    expect(siblingView("feed", -1, viewer)).toBe("messages");
    expect(siblingView("rooms", 1, { isSpectator: true })).toBe("feed");
    expect(siblingView("labs", 1, viewer)).toBe("season");
    expect(siblingView("badges", 1, viewer)).toBe("labs");
    expect(siblingView("overview", 1, viewer)).toBe("overview");
    expect(siblingView("agents", 1, viewer)).toBe("agents");
  });
});

describe("the stats section (You › Stats)", () => {
  it("is the agent's own: in You after Profile, at #/you/stats, never a spectator's", async () => {
    const { hashForView, routeFromHash } = await import("./dashboardRoute");
    const viewer = { isSpectator: false };
    const you = visibleDestinations(viewer).find((d) => d.id === "you")!.items.map((n) => n.id);
    expect(you).toEqual(["profile", "stats", "fees", "settings"]);
    expect(hashForView("stats")).toBe("#/you/stats");
    expect(routeFromHash("#/you/stats").view).toBe("stats");
    expect(routeFromHash("#/market/stats").view).toBe("stats");
    expect(siblingView("profile", 1, viewer)).toBe("stats");
    expect(navForHotkey("6", viewer, { you: "stats" })?.id).toBe("stats");
    expect(visibleNav({ isSpectator: true }).map((n) => n.id)).not.toContain("stats");
    expect(navForHotkey("6", { isSpectator: true }, { you: "stats" })?.id).toBe("profile");
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
    expect(more).toEqual(["relay", "operator", "docs"]);
  });
});
