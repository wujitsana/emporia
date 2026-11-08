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
 *   and, in the animatic style, the selected-row wash.
 *
 * The tests check ink and accent against every surface at ≥ 3:1 (WCAG non-text contrast),
 * for both styles, both themes and all eight tints, under both gamut treatments.
 */

import { TINT_COLORS, TINT_LABELS, TINTS, type Tint } from "../themeConfig.ts";
import { type Gamut, type RGB, hexToRgb, mixOver, relativeOklch } from "./color.ts";

export type StyleName = "animatic" | "retromod";
export type ThemeName = "light" | "dark";

/** The tint the dashboard falls back to when none is set ("Amber"). */
export const AMBER = TINT_COLORS[""];

/** The accent, exactly as badges.css writes it. Kept here so the test can hold the CSS to it. */
export const ACCENT_CSS: Record<ThemeName, string> = {
  light: "oklch(from var(--tint, #f0a832) 0.45 min(c, 0.2) h)",
  dark: "oklch(from var(--tint, #f0a832) max(l, 0.78) min(c, 0.22) h)",
};

export function accentFor(theme: ThemeName, tintHex: string, gamut: Gamut = "css"): RGB {
  return theme === "light"
    ? relativeOklch(tintHex, 0.45, (c) => Math.min(c, 0.2), gamut)
    : relativeOklch(tintHex, (l) => Math.max(l, 0.78), (c) => Math.min(c, 0.22), gamut);
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
  /** Animatic only: the fill chips and caption bars use, and the ink on it (for `mono` badges). */
  fill?: { bg: RGB; ink: RGB };
}

export function themeColors(style: StyleName, theme: ThemeName, tint: Tint, gamut: Gamut = "css"): ThemeColors {
  const hex = TINT_COLORS[tint] || AMBER;
  const rel = (L: number | ((v: number) => number), C: number | ((v: number) => number)) => relativeOklch(hex, L, C, gamut);
  const accent = accentFor(theme, hex, gamut);
  const base = { style, theme, tint, label: TINT_LABELS[tint], tintHex: hex, accent };
  if (style === "animatic") {
    const fill = rel((l) => Math.max(l, 0.86), (c) => c);
    const fillInk = rel(0.18, (c) => c * 0.4);
    if (theme === "light") {
      const bg = rel(0.97, (c) => c * 0.1);
      return { ...base, ink: rel(0.18, (c) => c * 0.4), fill: { bg: fill, ink: fillInk }, surfaces: [
        { name: "canvas", rgb: bg }, { name: "panel", rgb: rel(0.92, (c) => c * 0.18) },
        { name: "wash", rgb: mixOver(fill, 0.45, bg) }] };
    }
    const bg = rel(0.14, (c) => c * 0.18);
    return { ...base, ink: rel(0.94, (c) => c * 0.12), fill: { bg: fill, ink: fillInk }, surfaces: [
      { name: "canvas", rgb: bg }, { name: "panel", rgb: rel(0.24, (c) => c * 0.22) },
      { name: "wash", rgb: mixOver(fill, 0.14, bg) }] };
  }
  // retromod: no tint is plain white / black (global.css body.theme-*); a tint derives its ramp.
  if (theme === "light") {
    if (!tint) return { ...base, ink: hexToRgb("#000000"), surfaces: [
      { name: "canvas", rgb: hexToRgb("#ffffff") }, { name: "panel", rgb: hexToRgb("#e4e4e4") }] };
    return { ...base, ink: rel(0.18, (c) => c * 0.4), surfaces: [
      { name: "canvas", rgb: rel(0.97, (c) => c * 0.1) }, { name: "panel", rgb: rel(0.92, (c) => c * 0.18) }] };
  }
  if (!tint) return { ...base, ink: hexToRgb("#ffffff"), surfaces: [
    { name: "canvas", rgb: hexToRgb("#000000") }, { name: "panel", rgb: hexToRgb("#3a3a3a") }] };
  return { ...base, ink: rel(0.92, (c) => c * 0.18), surfaces: [
    { name: "canvas", rgb: rel(0.14, (c) => c * 0.18) }, { name: "panel", rgb: rel(0.24, (c) => c * 0.22) }] };
}

/** Every combination the dashboard can show: its one style × 2 themes × 8 tints. */
export function allThemes(gamut: Gamut = "css"): ThemeColors[] {
  const out: ThemeColors[] = [];
  for (const style of ["animatic"] as const) {
    for (const theme of ["light", "dark"] as const) {
      for (const tint of TINTS) out.push(themeColors(style, theme, tint, gamut));
    }
  }
  return out;
}
