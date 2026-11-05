// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  DEFAULT_STYLE,
  LS_STYLE,
  _resetStyleForTests,
  applyStyle,
  getStyle,
  nextStyle,
  readStyle,
  saveStyle,
  scrambleFrame,
  setStyle,
  styleFromParam,
} from "./styleMode";

function memory(initial: Record<string, string> = {}) {
  const m = new Map(Object.entries(initial));
  return {
    getItem: (k: string) => m.get(k) ?? null,
    setItem: (k: string, v: string) => void m.set(k, v),
    m,
  };
}

const broken = {
  getItem: () => { throw new Error("SecurityError: storage is blocked"); },
  setItem: () => { throw new Error("QuotaExceededError"); },
};

// jsdom here has no localStorage (see integrityCheck.test.ts): a small stand-in.
beforeEach(() => {
  const store = new Map<string, string>();
  vi.stubGlobal("localStorage", {
    getItem: (k: string) => store.get(k) ?? null,
    setItem: (k: string, v: string) => void store.set(k, v),
    removeItem: (k: string) => void store.delete(k),
    clear: () => store.clear(),
  });
});

afterEach(() => {
  vi.unstubAllGlobals();
  _resetStyleForTests();
});

describe("the style mode switch", () => {
  it("defaults to the current look", () => {
    expect(DEFAULT_STYLE).toBe("retromod");
    expect(readStyle({ storage: memory(), query: "" , capture: false })).toBe("retromod");
  });

  it("persists a choice and reads it back", () => {
    const s = memory();
    expect(saveStyle("animatic", s)).toBe(true);
    expect(s.m.get(LS_STYLE)).toBe("animatic");
    expect(readStyle({ storage: s, query: "", capture: false })).toBe("animatic");
    saveStyle("retromod", s);
    expect(readStyle({ storage: s, query: "", capture: false })).toBe("retromod");
  });

  it("survives bad storage: throwing, missing, or holding junk", () => {
    expect(readStyle({ storage: broken, query: "", capture: false })).toBe("retromod");
    expect(saveStyle("animatic", broken)).toBe(false);
    expect(readStyle({ storage: null, query: "", capture: false })).toBe("retromod");
    expect(saveStyle("animatic", null)).toBe(false);
    expect(readStyle({ storage: memory({ [LS_STYLE]: "<script>" }), query: "", capture: false })).toBe("retromod");
    expect(readStyle({ storage: memory({ [LS_STYLE]: "" }), query: "", capture: false })).toBe("retromod");
  });

  it("?style= wins for one load, and capture mode ignores what was saved", () => {
    const saved = memory({ [LS_STYLE]: "animatic" });
    expect(readStyle({ storage: memory(), query: "?style=animatic", capture: false })).toBe("animatic");
    expect(readStyle({ storage: saved, query: "?style=retromod", capture: false })).toBe("retromod");
    expect(readStyle({ storage: saved, query: "?style=default", capture: false })).toBe("retromod");
    expect(readStyle({ storage: saved, query: "?style=nonsense", capture: false })).toBe("animatic");
    expect(readStyle({ storage: saved, query: "", capture: true })).toBe("retromod");
    expect(readStyle({ storage: saved, query: "?capture=1&style=animatic", capture: true })).toBe("animatic");
  });

  it("reads the parameter by its plain name", () => {
    expect(styleFromParam("Animatic")).toBe("animatic");
    expect(styleFromParam("style-animatic")).toBe("animatic");
    expect(styleFromParam("console")).toBeNull();
    expect(styleFromParam("")).toBeNull();
    expect(styleFromParam(null)).toBeNull();
  });

  it("setStyle saves to localStorage and tells every reader", () => {
    expect(getStyle()).toBe("retromod");
    setStyle("animatic");
    expect(getStyle()).toBe("animatic");
    expect(localStorage.getItem(LS_STYLE)).toBe("animatic");
    _resetStyleForTests();
    expect(getStyle()).toBe("animatic");   // a fresh load reads it back
  });

  it("puts one class on the root for the animatic style, none for the default", () => {
    const root = document.createElement("div");
    root.className = "theme-light tint-green font-use-CommitMono";
    applyStyle("retromod", root);
    expect(root.className).toBe("theme-light tint-green font-use-CommitMono");
    expect(root.attributes.length).toBe(1);
    applyStyle("animatic", root);
    expect(root.classList.contains("style-animatic")).toBe(true);
    applyStyle("retromod", root);
    expect(root.className).toBe("theme-light tint-green font-use-CommitMono");
    expect(nextStyle("retromod")).toBe("animatic");
    expect(nextStyle("animatic")).toBe("retromod");
  });

  it("scrambles all but the resolved letters, and keeps spaces", () => {
    const zero = () => 0;
    expect(scrambleFrame("GAMES", 5, zero)).toBe("GAMES");
    expect(scrambleFrame("GAMES", 2, zero)).toBe("GA###");
    expect(scrambleFrame("A B", 0, zero)).toBe("# #");
  });
});
