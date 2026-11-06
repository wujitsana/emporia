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

    applyTheme("theme-light", "");
    expect(root.classList.contains("e-theme-anim")).toBe(false);

    applyTheme("theme-light", "tint-blue");
    expect(root.classList.contains("e-theme-anim")).toBe(true);
    expect(root.classList.contains("tint-blue")).toBe(true);

    // A run through the tints keeps it on until the last change settles.
    vi.advanceTimersByTime(THEME_FADE_MS - 100);
    applyTheme("theme-light", "tint-pink");
    vi.advanceTimersByTime(THEME_FADE_MS - 100);
    expect(root.classList.contains("e-theme-anim")).toBe(true);
    vi.advanceTimersByTime(200);
    expect(root.classList.contains("e-theme-anim")).toBe(false);

    applyTheme("theme-dark", "tint-pink");
    expect(root.classList.contains("e-theme-anim")).toBe(true);
    expect(root.classList.contains("theme-light")).toBe(false);
  });

  it("switches instantly when asked to (capture hotkeys), even mid-fade", () => {
    const root = document.createElement("div");
    setThemeRoot(root);
    applyTheme("theme-light", "");
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
