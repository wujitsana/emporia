import { describe, expect, it } from "vitest";
import { NAV, navForHotkey, visibleNav } from "./navConfig";

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
    expect(visibleNav({ isSpectator: false })).toHaveLength(NAV.length);
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
