// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ThemePanelBody } from "./ThemeControls";
import { TINTS, applyTheme, setThemeRoot, type Theme } from "./themeConfig";
import { INK_AA, normalizeInk, pureInk } from "./inkSwitch";
import { contrast } from "./badges/color.ts";
import { themeColors, type ThemeName } from "./badges/tokens.ts";

afterEach(() => { cleanup(); setThemeRoot(null); });

const THEMES: Theme[] = ["theme-light", "theme-vivid", "theme-dark"];

describe("the text switch: saved values", () => {
  it("maps the earlier Auto / White / Black choice to off / on / on, and junk to off", () => {
    expect(normalizeInk("auto")).toBe(false);
    expect(normalizeInk("white")).toBe(true);
    expect(normalizeInk("black")).toBe(true);
    expect(normalizeInk("on")).toBe(true);
    expect(normalizeInk("off")).toBe(false);
    expect(normalizeInk(null)).toBe(false);
    expect(normalizeInk("pink")).toBe(false);
  });
});

describe("the text switch: contrast guard", () => {
  it("is on only where pure ink reaches 4.5:1 on the canvas and the panel, in both gamuts", () => {
    let usable = 0;
    for (const theme of THEMES) for (const tint of TINTS) {
      const p = pureInk(theme, tint);
      const ink: [number, number, number] = p.color === "#ffffff" ? [1, 1, 1] : [0, 0, 0];
      let worst = Infinity;
      for (const gamut of ["css", "clip"] as const) {
        const t = themeColors("animatic", theme.replace("theme-", "") as ThemeName, tint, gamut);
        for (const s of t.surfaces.filter((x) => x.name !== "wash")) worst = Math.min(worst, contrast(ink, s.rgb));
      }
      expect(p.ok === (worst >= INK_AA && !p.why), `${theme} ${tint} ${worst.toFixed(2)} ${p.why}`).toBe(true);
      if (p.ok) usable++; else expect(p.why).not.toBe("");
    }
    expect(usable).toBe(TINTS.length * 3);   // the best of pure black / white always clears 4.5:1 here
    expect(pureInk("theme-light", "tint-green").color).toBe("#000000");
    expect(pureInk("theme-dark", "tint-green").color).toBe("#ffffff");
  });
});

describe("the text switch: control", () => {
  const base = { theme: "theme-dark" as const, tint: "tint-green" as const, onTheme: () => {}, onTint: () => {} };

  it("is a real switch with aria-checked, and toggles on click", () => {
    const onToggle = vi.fn();
    const { rerender } = render(<ThemePanelBody {...base} ink={{ on: false, disabledWhy: "", onToggle }} />);
    const sw = screen.getByRole("switch", { name: "Pure black or white text" });
    expect(sw.getAttribute("aria-checked")).toBe("false");
    fireEvent.click(sw);
    expect(onToggle).toHaveBeenCalledTimes(1);
    rerender(<ThemePanelBody {...base} ink={{ on: true, disabledWhy: "", onToggle }} />);
    expect(screen.getByRole("switch").getAttribute("aria-checked")).toBe("true");
    expect(screen.getAllByRole("button")).toHaveLength(3);   // the three circles; the switch is its own role
  });

  it("is disabled with a reason in its title where pure ink would be unreadable", () => {
    const onToggle = vi.fn();
    render(<ThemePanelBody {...base} ink={{ on: false, disabledWhy: "Pure black or white text would be low contrast here", onToggle }} />);
    const sw = screen.getByRole("switch") as HTMLButtonElement;
    expect(sw.disabled).toBe(true);
    expect(sw.getAttribute("title")).toMatch(/low contrast/);
    fireEvent.click(sw);
    expect(onToggle).not.toHaveBeenCalled();
  });
});

describe("the text switch: the page follows", () => {
  it("applyTheme puts the class and the ink on the root, and takes them off again", () => {
    const root = document.createElement("div");
    setThemeRoot(root);
    applyTheme("theme-dark", "tint-green", { fade: false, ink: "#ffffff" });
    expect(root.classList.contains("e-ink-pure")).toBe(true);
    expect(root.style.getPropertyValue("--e-pure-ink")).toBe("#ffffff");
    applyTheme("theme-dark", "tint-green", { fade: false, ink: null });
    expect(root.classList.contains("e-ink-pure")).toBe(false);
    expect(root.style.getPropertyValue("--e-pure-ink")).toBe("");
  });

  it("a card's text color follows the switch (the rules reach every token)", () => {
    const proc = (globalThis as unknown as { process: { getBuiltinModule(m: string): unknown } }).process;
    const fs = proc.getBuiltinModule("fs") as { readFileSync(p: string, enc: "utf8"): string };
    const css = fs.readFileSync("src/dashboard.css", "utf8");
    // The override block must reach the text, button and accent tokens, and vivid's ink.
    const block = css.slice(css.indexOf("body.e-look.e-ink-pure.e-ink-pure.e-ink-pure {"));
    expect(block).toMatch(/--theme-text: var\(--e-pure-ink\)/);
    expect(block).toMatch(/--theme-button: var\(--e-pure-ink\)/);
    expect(block).toMatch(/--theme-focused-foreground: var\(--e-pure-ink\)/);
    expect(css).toMatch(/body\.e-look\.theme-vivid\.e-ink-pure\.e-ink-pure\.e-ink-pure \{ --vv-ink: var\(--e-pure-ink\); \}/);
    // jsdom does not resolve var(): the computed color of a card under the real rules is checked in Chrome (ui-look-fix/ink-check.json).
  });
});
