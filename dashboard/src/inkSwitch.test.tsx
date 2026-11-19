// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ThemePanelBody } from "./ThemeControls";
import { TINTS, applyTheme, setThemeRoot, type Theme } from "./themeConfig";
import { FLIP_MIN, INK_AA, normalizeInk, pureInk } from "./inkSwitch";

afterEach(() => { cleanup(); setThemeRoot(null); });


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

describe("the text switch: a flip, in vivid only", () => {
  it("is not shown in light or dark (their ink is already near-black or near-white)", () => {
    for (const tint of TINTS) for (const theme of ["theme-light", "theme-dark"] as Theme[]) {
      const p = pureInk(theme, tint);
      expect(p.shown, `${theme} ${tint}`).toBe(false);
      expect(p.ok).toBe(false);
    }
  });

  it("flips vivid's own ink to the other of black and white", () => {
    expect(pureInk("theme-vivid", "tint-red").color).toBe("#000000");      // white on crimson -> black
    expect(pureInk("theme-vivid", "tint-blue").color).toBe("#000000");
    expect(pureInk("theme-vivid", "tint-purple").color).toBe("#000000");
    expect(pureInk("theme-vivid", "tint-orange").color).toBe("#ffffff");   // dark on orange -> white
    expect(pureInk("theme-vivid", "tint-charcoal").color).toBe("#ffffff");
  });

  it("is usable on every vivid tint but green, where white would be unreadable, and says why", () => {
    for (const tint of TINTS) {
      const p = pureInk("theme-vivid", tint);
      expect(p.shown).toBe(true);
      expect(p.ok === (p.worst >= FLIP_MIN), `${tint} ${p.worst.toFixed(2)}`).toBe(true);
      if (!p.ok) expect(p.why).toMatch(/unreadable/);
    }
    expect(pureInk("theme-vivid", "tint-green").ok).toBe(false);
    expect(pureInk("theme-vivid", "tint-red").ok).toBe(true);
    // a style choice: the flipped ink stays under the 4.5:1 body-text bar on every tint
    for (const tint of TINTS) expect(pureInk("theme-vivid", tint).worst).toBeLessThan(INK_AA);
  });
});

describe("the text switch: control", () => {
  const base = { theme: "theme-vivid" as const, tint: "tint-green" as const, onTheme: () => {}, onTint: () => {} };

  it("is a real switch with aria-checked, and toggles on click", () => {
    const onToggle = vi.fn();
    const { rerender } = render(<ThemePanelBody {...base} ink={{ on: false, disabledWhy: "", onToggle }} />);
    const sw = screen.getByRole("switch", { name: "Flip the text: black or white" });
    expect(sw.getAttribute("aria-checked")).toBe("false");
    fireEvent.click(sw);
    expect(onToggle).toHaveBeenCalledTimes(1);
    rerender(<ThemePanelBody {...base} ink={{ on: true, disabledWhy: "", onToggle }} />);
    expect(screen.getByRole("switch").getAttribute("aria-checked")).toBe("true");
    expect(screen.getAllByRole("button")).toHaveLength(3);   // the three circles; the switch is its own role
  });

  it("is disabled with a reason in its title where pure ink would be unreadable", () => {
    const onToggle = vi.fn();
    render(<ThemePanelBody {...base} ink={{ on: false, disabledWhy: "White text would be unreadable on this vivid tint (1.3:1)", onToggle }} />);
    const sw = screen.getByRole("switch") as HTMLButtonElement;
    expect(sw.disabled).toBe(true);
    expect(sw.getAttribute("title")).toMatch(/unreadable/);
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
