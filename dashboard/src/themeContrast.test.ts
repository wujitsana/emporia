/**
 * The three themes × seven tints, computed from the palette table (themePalette.ts) the CSS mirrors:
 * text and accent-as-text reach WCAG AA (4.5:1) on the canvas and the panel, chips reach it too, and
 * dashboard.css / the kit's global.css / badges.css carry the same numbers. Set THEME_CONTRAST_TABLE=1
 * to print the table.
 */
import { describe, expect, it } from "vitest";
import { contrast, type Gamut } from "./badges/color.ts";
import { TINTS, TINT_LABELS } from "./themeConfig.ts";
import { WHITE_INK, LIGHT_PANEL, rgbHex, deepPalette, vividPalette } from "./themePalette.ts";
import { themeColors, type ThemeName } from "./badges/tokens.ts";

type Fs = { readFileSync(p: string, enc: "utf8"): string };
const proc = (globalThis as unknown as { process: { cwd(): string; getBuiltinModule(m: string): unknown } }).process;
const fs = proc.getBuiltinModule("node:fs") as Fs;
const read = (f: string) => fs.readFileSync(`${proc.cwd()}/src/${f}`, "utf8");
const DASH = read("dashboard.css");
const KIT = read("ui/kit/global.css");

const AA = 4.5;
const THEMES: ThemeName[] = ["light", "vivid", "dark"];

describe("contrast of every tint in every theme", () => {
  for (const gamut of ["css", "clip"] as Gamut[]) {
    it(`text, accent text and chips reach ${AA}:1 on the canvas and the panel (${gamut} gamut)`, () => {
      const fails: string[] = [];
      for (const theme of THEMES) for (const tint of TINTS) {
        const t = themeColors("animatic", theme, tint, gamut);
        for (const s of t.surfaces.filter((x) => x.name !== "wash")) {
          for (const [what, rgb] of [["text", t.ink], ["accent text", t.accent]] as const) {
            const r = contrast(rgb, s.rgb);
            if (r < AA) fails.push(`${theme} ${TINT_LABELS[tint]} ${what} on ${s.name}: ${r.toFixed(2)}`);
          }
        }
        const chip = contrast(t.fill.ink, t.fill.bg);
        if (chip < AA) fails.push(`${theme} ${TINT_LABELS[tint]} chip: ${chip.toFixed(2)}`);
      }
      expect(fails).toEqual([]);
    });
  }

  it("prints the table when asked", () => {
    const rows: string[] = ["theme | tint | canvas | panel | text | accent | text/canvas | text/panel | accent/canvas | accent/panel | chip"];
    for (const theme of ["vivid", "dark"] as const) for (const tint of TINTS) {
      const p = (theme === "vivid" ? vividPalette : deepPalette)(tint);
      rows.push([theme, TINT_LABELS[tint], rgbHex(p.bg), rgbHex(p.panel), rgbHex(p.text), rgbHex(p.accent),
        contrast(p.text, p.bg).toFixed(2), contrast(p.text, p.panel).toFixed(2),
        contrast(p.accent, p.bg).toFixed(2), contrast(p.accent, p.panel).toFixed(2), contrast(p.fillInk, p.fill).toFixed(2)].join(" | "));
    }
    expect(rows).toHaveLength(15);
    if ((proc as unknown as { env: Record<string, string> }).env.THEME_CONTRAST_TABLE) console.info(rows.join("\n"));
  });
});

describe("the green themes are the video's exact colours", () => {
  it("vivid green: #39ff44 / #2ee03a / #001b00", () => {
    const p = vividPalette("tint-green");
    expect([p.bg, p.panel, p.text, p.border, p.accent].map(rgbHex)).toEqual(["#39ff44", "#2ee03a", "#001b00", "#001b00", "#001b00"]);
  });
  it("deep green: #001b00 / #0b2a0b / #1d4a1d / #d8ffd8, accent #39ff44", () => {
    const p = deepPalette("tint-green");
    expect([p.bg, p.panel, p.border, p.text, p.accent].map(rgbHex)).toEqual(["#001b00", "#0b2a0b", "#1d4a1d", "#d8ffd8", "#39ff44"]);
  });
});

describe("the CSS carries the palette", () => {
  it("dashboard.css: deep green is pinned, the generic deep formula and the vivid one are written", () => {
    expect(DASH).toMatch(/body\.e-look\.theme-dark\.tint-green \{[^}]*--theme-background: #001b00;[^}]*--theme-background-modal: #0b2a0b;[^}]*--theme-border: #1d4a1d;[^}]*--theme-text: #d8ffd8;[^}]*--theme-focused-foreground: #39ff44;/);
    expect(DASH).toContain("--theme-background: oklch(from var(--anim-tint) 0.192 calc(c * 0.243) h);");
    expect(DASH).toContain("--theme-background-modal: oklch(from var(--anim-tint) 0.253 calc(c * 0.243) h);");
    expect(DASH).toContain("--theme-border: oklch(from var(--anim-tint) 0.366 calc(c * 0.328) h);");
    expect(DASH).toContain("--theme-text: oklch(from var(--anim-tint) 0.962 calc(c * 0.243) h);");
    expect(DASH).toContain("--vv-panel: oklch(from var(--anim-tint) calc(l - 0.082) calc(c * 0.91) h);");
    expect(DASH).toMatch(/body\.e-look\.theme-vivid\.tint-green \{ --vv-ink: #001b00; --vv-panel: #2ee03a; \}/);
  });

  it("the vivid rules name the same tints for white ink and the lighter panel as the table", () => {
    expect(WHITE_INK).toEqual(["tint-blue", "tint-red", "tint-purple"]);
    expect(LIGHT_PANEL).toEqual(["tint-orange", "tint-pink"]);
    expect(DASH).toContain("body.e-look.theme-vivid.tint-blue,\nbody.e-look.theme-vivid.tint-red,\nbody.e-look.theme-vivid.tint-purple { --vv-ink: #fff; }");
    expect(DASH).toContain("body.e-look.theme-vivid.tint-orange,\nbody.e-look.theme-vivid.tint-pink { --vv-panel: color-mix(in srgb, var(--anim-tint) 80%, white); }");
  });

  it("the kit's global.css has the same three ramps for every tint", () => {
    for (const t of TINTS) {
      expect(KIT).toContain(`body.theme-vivid.${t}`);
      expect(KIT).toContain(`body.theme-dark.${t}`);
      expect(KIT).toContain(`body.theme-light.${t}`);
    }
    expect(KIT).toContain("--theme-background: oklch(from var(--tint) 0.192 calc(c * 0.243) h);");
    expect(KIT).toContain("body.theme-vivid.tint-green { --vv-ink: #001b00; --vv-panel: #2ee03a; }");
    expect(KIT).toMatch(/body\.theme-dark\.tint-green \{[^}]*--theme-text: #d8ffd8;[^}]*--theme-focused-foreground: #39ff44;/);
  });

  it("no amber is left as a colour in the look, the kit, the badges or the theme config", () => {
    for (const f of ["dashboard.css", "ui/kit/global.css", "badges/badges.css", "badges/tokens.ts", "themeConfig.ts"]) {
      expect(read(f), f).not.toMatch(/#f0a832|#f5b84a/i);
    }
    expect(read("badges/tokens.ts")).not.toMatch(/\bAMBER\b/);
  });
});
