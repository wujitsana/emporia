/**
 * The colours a badge meets, per style, theme and tint, computed the way the CSS does.
 *
 * - Ink is `currentColor`: the theme's text colour where the badge sits (`--theme-text`).
 * - The accent is `--e-badge-accent` (badges.css), derived from `--tint` like the
 *   look's accent-as-text (`--theme-focused-foreground`, dashboard.css § 30): dark
 *   enough on the light canvas, light enough on the dark one, for every tint. Since
 *   ui-v2 the look is the only style and badges.css points the accent at that token;
 *   the retromod ramps stay here as history, and are no longer checked.
 * - The surfaces are what a badge can sit on: the canvas, a panel (`--theme-background-modal`)
 *   and the selected-row wash.
 * - Vivid and deep (the dark theme) come from ../themePalette.ts, the table the CSS is held to.
 *
 * The tests check ink and accent against every surface at ≥ 3:1 (WCAG non-text contrast),
 * for all three themes and all six tints, under both gamut treatments.
 */

import { TINT_COLORS, TINT_LABELS, TINTS, DEFAULT_TINT, type Tint } from "../themeConfig.ts";
import { type Gamut, type RGB, hexToRgb, mixOver, relativeOklch } from "./color.ts";
import { COMPANIONS } from "../palette.ts";
import { LIGHT_NEUTRAL, deepPalette, vividPalette } from "../themePalette.ts";

export type StyleName = "animatic";
export type ThemeName = "light" | "vivid" | "dark";

/** The tint the dashboard falls back to when none is set (green, the default). */
export const DEFAULT_TINT_HEX = TINT_COLORS[DEFAULT_TINT];

/** The accent, exactly as badges.css writes it. Kept here so the test can hold the CSS to it. */
export const ACCENT_CSS: Record<ThemeName, string> = {
  light: "var(--tint-deep, #006c00)",
  vivid: "oklch(from var(--tint, #39ff44) 0.192 calc(c * 0.243) h)",
  dark: "var(--tint-light, #39ff44)",
};

export function accentFor(theme: ThemeName, tint: Tint, gamut: Gamut = "css"): RGB {
  if (theme === "vivid") return vividPalette(tint, gamut).accent;
  if (theme === "dark") return deepPalette(tint, gamut).accent;
  return hexToRgb(COMPANIONS[tint].deep);
}

export interface Surface { name: string; rgb: RGB }

export interface ThemeColors {
  style: StyleName;
  theme: ThemeName;
  tint: Tint;
  label: string;
  tintHex: string;
  ink: RGB;
  accent: RGB;
  surfaces: Surface[];
  /** The fill chips and caption bars use, and the ink on it (for `mono` badges). */
  fill: { bg: RGB; ink: RGB };
}

/** Share of the fill laid over the canvas for the selected-row wash (dashboard.css § 30); vivid's and dark's wash is the panel. */
export const WASH = { light: 0.45 } as const;

export function themeColors(style: StyleName, theme: ThemeName, tint: Tint, gamut: Gamut = "css"): ThemeColors {
  const hex = TINT_COLORS[tint] || DEFAULT_TINT_HEX;
  const base = { style, theme, tint, label: TINT_LABELS[tint], tintHex: hex };
  if (theme === "light") {
    const rel = (L: number | ((v: number) => number), C: number | ((v: number) => number)) => relativeOklch(hex, L, C, gamut);
    const fill = rel((l) => Math.max(l, 0.86), (c) => c);
    const neutral = LIGHT_NEUTRAL[tint];
    const bg = neutral ? relativeOklch(neutral.bg, (l) => l, (c) => c, gamut) : rel(0.97, (c) => c * 0.1);
    return { ...base, accent: accentFor(theme, tint, gamut), ink: rel(0.18, (c) => c * 0.4),
      fill: { bg: fill, ink: rel(0.18, (c) => c * 0.4) }, surfaces: [
        { name: "canvas", rgb: bg }, { name: "panel", rgb: neutral ? relativeOklch(neutral.panel, (l) => l, (c) => c, gamut) : rel(0.92, (c) => c * 0.18) },
        { name: "wash", rgb: mixOver(fill, WASH.light, bg) }] };
  }
  const p = theme === "vivid" ? vividPalette(tint, gamut) : deepPalette(tint, gamut);
  return { ...base, accent: p.accent, ink: p.text, fill: { bg: p.fill, ink: p.fillInk }, surfaces: [
    { name: "canvas", rgb: p.bg }, { name: "panel", rgb: p.panel },
    { name: "wash", rgb: p.panel }] };   // vivid and dark: the hover / selected wash is the panel
}

/** Every combination the dashboard can show: its one style × 3 themes × 6 tints. */
export function allThemes(gamut: Gamut = "css"): ThemeColors[] {
  const out: ThemeColors[] = [];
  for (const theme of ["light", "vivid", "dark"] as const) {
    for (const tint of TINTS) out.push(themeColors("animatic", theme, tint, gamut));
  }
  return out;
}
