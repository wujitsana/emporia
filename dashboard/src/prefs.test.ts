// @vitest-environment jsdom
import { describe, expect, it, vi } from "vitest";
import { clearRemovedPrefs, readPref, writePref } from "./prefs";

const mem = new Map<string, string>();
vi.stubGlobal("localStorage", {
  getItem: (k: string) => mem.get(k) ?? null,
  setItem: (k: string, v: string) => void mem.set(k, v),
  removeItem: (k: string) => void mem.delete(k),
});

describe("removed look switches", () => {
  it("their saved values are dropped and the theme keys stay", () => {
    writePref("emporia_font", "serif"); writePref("emporia_view_mode", "list"); writePref("emporia_tint", "tint-red");
    clearRemovedPrefs();
    expect(readPref("emporia_font")).toBeNull();
    expect(readPref("emporia_view_mode")).toBeNull();
    expect(readPref("emporia_tint")).toBe("tint-red");
  });
});
