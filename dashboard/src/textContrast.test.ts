/**
 * Review pass 2: text contrast in the one look (dashboard.css § 30), for both
 * themes and all eight tints.
 *
 * - The look's text tokens reach WCAG AA (4.5:1) on the canvas, computed from
 *   the CSS formulas (badges/tokens.ts mirrors them) under both gamut treatments.
 * - No text is dimmed by opacity below 0.6 without the look turning it into the
 *   faint colour: at 0.45 a label measured 2.9:1 on the light green canvas, at
 *   0.55 4.0:1. A rule that dims graphics or a state that is out of play
 *   (a struck-out seat, an empty cell) says so in GRAPHICS_OR_INACTIVE.
 */
import postcss, { type Rule } from "postcss";
import { describe, expect, it } from "vitest";
import { contrast, mixOver, type Gamut } from "./badges/color.ts";
import { allThemes } from "./badges/tokens.ts";

type Fs = { readFileSync(p: string, enc: "utf8"): string };
const proc = (globalThis as unknown as { process: { cwd(): string; getBuiltinModule(m: string): unknown } }).process;
const fs = proc.getBuiltinModule("node:fs") as Fs;
const CSS = fs.readFileSync(`${proc.cwd()}/src/dashboard.css`, "utf8");
const CHAT = fs.readFileSync(`${proc.cwd()}/src/chat/ChatDrawer.module.css`, "utf8");

const AA = 4.5;

describe("the look's text tokens", () => {
  for (const gamut of ["css", "clip"] as Gamut[]) {
    it(`reach ${AA}:1 on the canvas in both themes and all 8 tints (${gamut} gamut)`, () => {
      const fails: string[] = [];
      for (const t of allThemes(gamut)) {
        const canvas = t.surfaces.find((s) => s.name === "canvas")!.rgb;
        const wash = t.surfaces.find((s) => s.name === "wash")!.rgb;
        const checks: [string, number][] = [
          ["text", contrast(t.ink, canvas)],
          ["faint (68 %)", contrast(mixOver(t.ink, 0.68, canvas), canvas)],
          ["status line (80 %)", contrast(mixOver(t.ink, 0.8, canvas), canvas)],
          ["accent as text", contrast(t.accent, canvas)],
          ["canvas on the accent (badges, send)", contrast(canvas, t.accent)],
          ["ink on the fill", contrast(t.fill!.ink, t.fill!.bg)],
          ["text on the wash", contrast(t.ink, wash)],
        ];
        for (const [what, r] of checks) if (r < AA) fails.push(`${t.theme} ${t.label} ${what}: ${r.toFixed(2)}`);
      }
      expect(fails).toEqual([]);
    });
  }
});

/** Dimmed on purpose, and not text that must be read: graphics, or a state out of play. */
const GRAPHICS_OR_INACTIVE = new Set([
  '.e-dashboard-shell [class*="SidebarLayout_sidebar"] [class*="Divider_line"]',
  ".e-relay-axis", ".e-numline__axis", ".e-numline__tick", ".e-numline__mark",
  ".e-seat.is-out", ".e-lowest__cell.is-empty",
  // the base step: each state a step can be in has its own rule in the look
  ".e-phase-strip__step",
  // the look sets every key hint to 80 % ink (.e-look .e-section-tab__key)
  ".e-section-tabs--drawer .e-section-tab__key",
  // classes no component renders any more (proposal P-dead-css)
  ".e-nav-row-hotkey", ".e-sidebar-group-label", ".e-sidebar-key__k", ".e-nav-card__hotkey",
  ".e-stat-strip__lbl", ".e-feed-jump", ".e-mode-toggle", ".e-vote-btn", ".e-status-pill--off",
]);

function walk(css: string) {
  const dimmed: { sel: string; value: number }[] = [];
  const lookFaint = new Set<string>();
  const lookOverride = new Set<string>();
  postcss.parse(css).walkRules((rule: Rule) => {
    const parent = rule.parent as { type?: string; name?: string };
    if (parent?.type === "atrule" && parent.name === "keyframes") return;
    const sels = rule.selectors.map((s) => s.trim());
    let opacity: number | null = null;
    let faint = false;
    rule.walkDecls((d) => {
      if (d.prop === "opacity") opacity = parseFloat(d.value);
      if (d.prop === "color" && /--anim-faint|68%/.test(d.value)) faint = true;
    });
    for (const sel of sels) {
      if (sel.startsWith(".e-look ")) {
        const inner = sel.slice(".e-look ".length);
        if (faint) lookFaint.add(inner);
        if (opacity !== null && opacity >= 0.6) lookOverride.add(inner);
      } else if (opacity !== null && opacity < 0.6) {
        dimmed.push({ sel, value: opacity });
      }
    }
  });
  return { dimmed, lookFaint, lookOverride };
}

describe("no text dimmed below AA by opacity", () => {
  it("dashboard.css: every rule under 0.6 opacity is made faint by the look, or is graphics", () => {
    const { dimmed, lookFaint, lookOverride } = walk(CSS);
    const bad = dimmed.filter((d) => !lookFaint.has(d.sel) && !lookOverride.has(d.sel) && !GRAPHICS_OR_INACTIVE.has(d.sel)
      && d.sel !== ".e-dim" && d.sel !== ".e-faint" && d.sel !== ".e-badge--dim");
    expect(bad.map((d) => `${d.sel} { opacity: ${d.value} }`)).toEqual([]);
  });

  it("the chat's speaker, tool time and session lines are faint as a colour, not by opacity", () => {
    const { dimmed } = walk(CHAT);
    const text = dimmed.map((d) => d.sel).filter((s) => [".who", ".toolTime", ".sessionMeta"].includes(s));
    expect(text).toEqual([]);
  });
});
