// @vitest-environment jsdom
/**
 * One style (ui-v2): the animatic look is the dashboard's, with no switch. Light/dark
 * and the eight tints stay the viewer's (t, T, ?tint=, ?theme=).
 */
import { afterEach, describe, expect, it } from "vitest";

import { scrambleFrame } from "./look";
import { applyTheme } from "./themeConfig";

// No node types in this tsconfig: Node's own fs, typed narrowly (as cssModuleName.test.ts does).
type Fs = { readFileSync(p: string, enc: "utf8"): string; readdirSync(p: string): string[] };
const proc = (globalThis as unknown as { process: { cwd(): string; getBuiltinModule(m: string): unknown } }).process;
const fs = proc.getBuiltinModule("node:fs") as Fs;
const SRC = `${proc.cwd()}/src`;
const CSS = fs.readFileSync(`${SRC}/dashboard.css`, "utf8");

afterEach(() => {
  document.body.className = "";
});

describe("the look", () => {
  it("has no style switch left: no class, no file, no setting", () => {
    expect(CSS).not.toMatch(/style-animatic|e-style-switch/);
    const src = fs.readdirSync(SRC);
    expect(src).not.toContain("styleMode.ts");
    expect(src).not.toContain("styleAnimatic.css");
  });

  it("goes on the app's root with the theme and tint, in every theme and tint", () => {
    applyTheme("theme-dark", "tint-blue", { fade: false });
    expect([...document.body.classList]).toEqual(expect.arrayContaining(["e-look", "theme-dark", "tint-blue"]));
    applyTheme("theme-light", "", { fade: false });
    expect(document.body.classList.contains("e-look")).toBe(true);
  });

  it("darkens the accent used as text on the light canvas and keeps faint text a colour (readability)", () => {
    expect(CSS).toMatch(/body\.e-look\.theme-light \{[^}]*--theme-focused-foreground: oklch\(from var\(--anim-tint\) 0\.45/);
    expect(CSS).toMatch(/--anim-faint: color-mix\(in srgb, var\(--theme-text\) 68%/);
  });

  it("loads no remote font: the pixel face is bundled", () => {
    expect(CSS).not.toMatch(/url\(\s*['"]?(https?:)?\/\//i);
    expect(CSS).toMatch(/url\("\.\/assets\/fonts\/Silkscreen-Regular\.woff2"\)/);
  });

  it("scrambles all but the resolved letters, and keeps spaces", () => {
    const f = scrambleFrame("AB CD", 2, () => 0);
    expect(f.slice(0, 3)).toBe("AB ");
    expect(f).toHaveLength(5);
    expect(f[3]).not.toBe("C");
  });
});
