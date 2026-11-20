// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { THEME_FADE_MS, applyTheme, setThemeRoot } from "./themeConfig";

describe("theme cross-fade", () => {
  afterEach(() => {
    setThemeRoot(null);
    vi.useRealTimers();
  });

  it("does not fade the first paint, fades a change, and stops fading after it settles", () => {
    vi.useFakeTimers();
    const root = document.createElement("div");
    setThemeRoot(root);

    applyTheme("theme-light", "tint-green");
    expect(root.classList.contains("e-theme-anim")).toBe(false);

    applyTheme("theme-light", "tint-blue");
    expect(root.classList.contains("e-theme-anim")).toBe(true);
    expect(root.classList.contains("tint-blue")).toBe(true);

    // A run through the tints keeps it on until the last change settles.
    vi.advanceTimersByTime(THEME_FADE_MS - 100);
    applyTheme("theme-light", "tint-orange");
    vi.advanceTimersByTime(THEME_FADE_MS - 100);
    expect(root.classList.contains("e-theme-anim")).toBe(true);
    vi.advanceTimersByTime(200);
    expect(root.classList.contains("e-theme-anim")).toBe(false);

    applyTheme("theme-dark", "tint-orange");
    expect(root.classList.contains("e-theme-anim")).toBe(true);
    expect(root.classList.contains("theme-light")).toBe(false);
  });

  it("switches instantly when asked to (capture hotkeys), even mid-fade", () => {
    const root = document.createElement("div");
    setThemeRoot(root);
    applyTheme("theme-light", "tint-green");
    applyTheme("theme-light", "tint-red");
    expect(root.classList.contains("e-theme-anim")).toBe(true);
    applyTheme("theme-light", "tint-blue", { fade: false, capture: true });
    expect(root.classList.contains("e-theme-anim")).toBe(false);
    expect(root.classList.contains("e-capture")).toBe(true);
    expect(root.classList.contains("tint-blue")).toBe(true);
  });

  it("re-applying the same theme and tint does not fade", () => {
    const root = document.createElement("div");
    setThemeRoot(root);
    applyTheme("theme-dark", "tint-green");
    applyTheme("theme-dark", "tint-green");
    expect(root.classList.contains("e-theme-anim")).toBe(false);
  });
});

import { DEFAULT_TINT, THEMES, TINTS, TINT_COLORS, TINT_LABELS, nextTint, normalizeBase, normalizeTheme, normalizeTint, toggleTheme, toggleVivid } from "./themeConfig";

describe("tints", () => {
  it("are six, green first and the default, with no amber (the orange is burnt, #d57c22, the red crimson)", () => {
    expect([...TINTS]).toEqual(["tint-green", "tint-blue", "tint-red", "tint-orange", "tint-purple", "tint-charcoal"]);
    expect(DEFAULT_TINT).toBe("tint-green");
    expect(TINT_COLORS["tint-green"]).toBe("#2ae13f");
    expect(Object.values(TINT_LABELS)).not.toContain("Amber");
    expect(Object.values(TINT_COLORS)).not.toContain("#f0a832");
  });

  it("cycle through all five and back to green", () => {
    let t = DEFAULT_TINT;
    const seen: string[] = [t];
    for (let i = 0; i < TINTS.length; i++) { t = nextTint(t); seen.push(t); }
    expect(seen.slice(0, TINTS.length)).toEqual([...TINTS]);
    expect(seen[TINTS.length]).toBe("tint-green");
  });

  it("load from a saved preference: the old default, amber and junk are green", () => {
    for (const old of ["", "tint-amber", "amber", "tint-pink", "pink", "tint-yellow", "yellow", "tint-teal", null, undefined]) expect(normalizeTint(old)).toBe("tint-green");
    for (const t of TINTS) expect(normalizeTint(t)).toBe(t);
  });
});

describe("themes", () => {
  it("are light, vivid, dark", () => {
    expect([...THEMES]).toEqual(["theme-light", "theme-vivid", "theme-dark"]);
  });

  it("the mode circle swaps light and dark; from vivid it leaves for the other of the last two", () => {
    expect(toggleTheme("theme-light")).toBe("theme-dark");
    expect(toggleTheme("theme-dark")).toBe("theme-light");
    expect(toggleTheme("theme-vivid", "theme-light")).toBe("theme-dark");
    expect(toggleTheme("theme-vivid", "theme-dark")).toBe("theme-light");
  });

  it("the vivid circle turns vivid on, and off back to the last of light / dark", () => {
    expect(toggleVivid("theme-light", "theme-light")).toBe("theme-vivid");
    expect(toggleVivid("theme-dark", "theme-dark")).toBe("theme-vivid");
    expect(toggleVivid("theme-vivid", "theme-dark")).toBe("theme-dark");
    expect(toggleVivid("theme-vivid", "theme-light")).toBe("theme-light");
  });

  it("load from a saved preference: unknown is light; a saved base falls back to the theme's own", () => {
    expect(normalizeTheme("theme-vivid")).toBe("theme-vivid");
    expect(normalizeTheme("theme-dark")).toBe("theme-dark");
    expect(normalizeTheme("")).toBe("theme-light");
    expect(normalizeTheme(null)).toBe("theme-light");
    expect(normalizeBase("theme-dark", "theme-vivid")).toBe("theme-dark");
    expect(normalizeBase(null, "theme-dark")).toBe("theme-dark");
    expect(normalizeBase("theme-vivid", "theme-vivid")).toBe("theme-light");
  });

  it("applyTheme puts the vivid class on, the old amber class off, and an empty tint is green", () => {
    const root = document.createElement("div");
    setThemeRoot(root);
    root.classList.add("tint-amber");
    applyTheme("theme-vivid", "", { fade: false });
    expect(root.classList.contains("theme-vivid")).toBe(true);
    expect(root.classList.contains("tint-green")).toBe(true);
    expect(root.classList.contains("tint-amber")).toBe(false);
    setThemeRoot(null);
  });
});
