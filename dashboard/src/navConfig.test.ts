import { describe, expect, it } from "vitest";
import { groupedNav, NAV, NAV_GROUPS, navForHotkey, visibleNav } from "./navConfig";

// The tabs and the menu are drawn from groupedNav: every section the viewer may
// reach appears exactly once, in group order, and nothing hidden sneaks back in.
describe("groupedNav", () => {
  it("places every section in exactly one group", () => {
    const grouped = NAV_GROUPS.flatMap((g) => g.ids);
    expect(new Set(grouped).size).toBe(grouped.length);
    expect([...grouped].sort()).toEqual(NAV.map((n) => n.id).sort());
  });

  it("shows each visible section once, and only those", () => {
    for (const opts of [{ isSpectator: true }, { isSpectator: false }, { isSpectator: false, isOperator: true }]) {
      const visible = visibleNav(opts);
      const drawn = groupedNav(visible).flatMap((g) => g.items.map((n) => n.id));
      expect([...drawn].sort()).toEqual(visible.map((n) => n.id).sort());
    }
  });

  it("starts with the overview and the season, and drops empty groups", () => {
    const groups = groupedNav(visibleNav({ isSpectator: true }));
    expect(groups[0].items.map((n) => n.id)).toEqual(["overview", "gamify"]);
    expect(groups.every((g) => g.items.length > 0)).toBe(true);
  });

  it("keeps a section missing from the groups reachable", () => {
    const extra = { id: "games" as const, label: "Games", hotkey: "x", icon: "x", blurb: "x" };
    const groups = groupedNav([...visibleNav({ isSpectator: false }), extra]);
    expect(groups.at(-1)?.items.map((n) => n.id)).toEqual(["games"]);
  });

  it("gives every section a plain-words blurb for its heading", () => {
    for (const n of NAV) expect(n.blurb.length).toBeGreaterThan(20);
  });
});

// P35-2: nav visibility was filtered in AppNav.useVisibleNav(), but App.tsx's
// keyboard handler looked the hotkey up in the unfiltered NAV. So the two
// sections the nav deliberately hides from a spectator were both still one
// keypress away -- and they failed differently, which is why neither was
// noticed: "0" (fees) reached `view === "fees" && !isSpectator` and rendered a
// blank pane, while "8" (messages) had no such guard at the render site and
// rendered the whole view.

describe("visibleNav", () => {
  it("hides fees and messages from a spectator", () => {
    const ids = visibleNav({ isSpectator: true }).map((n) => n.id);
    expect(ids).not.toContain("fees");
    expect(ids).not.toContain("messages");
  });

  it("shows every section to an identified viewer", () => {
    expect(visibleNav({ isSpectator: false, isOperator: true })).toHaveLength(NAV.length);
    expect(visibleNav({ isSpectator: false }).map((n) => n.id)).not.toContain("relay");
  });
});

describe("navForHotkey", () => {
  it("refuses a spectator the hotkeys for sections the nav hides", () => {
    expect(navForHotkey("0", { isSpectator: true })).toBeUndefined();
    expect(navForHotkey("8", { isSpectator: true })).toBeUndefined();
  });

  it("still resolves those hotkeys for an identified viewer", () => {
    expect(navForHotkey("0", { isSpectator: false })?.id).toBe("fees");
    expect(navForHotkey("8", { isSpectator: false })?.id).toBe("messages");
  });

  it("resolves shared hotkeys the same for both", () => {
    for (const spec of [true, false]) {
      expect(navForHotkey("1", { isSpectator: spec })?.id).toBe("overview");
      expect(navForHotkey("3", { isSpectator: spec })?.id).toBe("sessions");
    }
  });

  it("reaches exactly the sections the nav shows -- no third answer", () => {
    for (const isSpectator of [true, false]) {
      const shown = visibleNav({ isSpectator }).map((n) => n.id);
      const reachable = NAV.map((n) => navForHotkey(n.hotkey, { isSpectator }))
        .filter((n): n is NonNullable<typeof n> => !!n)
        .map((n) => n.id);
      expect(reachable).toEqual(shown);
    }
  });
});
