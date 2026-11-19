/**
 * The vivid and deep palettes, worked out per tint the way dashboard.css § 30 (and the kit's
 * global.css) write them. One table of formulas here; the contrast tests and the control's
 * circles read it, and a test holds the CSS to the same numbers. The ui-style-experimental design note (kept outside the repo).
 *
 * - vivid: the tint is the background, ink is the tint's deep dark (white on blue and purple),
 *   the panel a step darker (a step lighter for orange, whose ink is darkest against it).
 *   Green is pinned exactly: bg #39ff44, panel #2ee03a, ink #001b00.
 * - deep (the dark theme): the video's all-dark-green screens in OKLCH relative to the tint
 *   (bg L .192 C×.243, panel L .253 C×.243, border L .366 C×.328, text L .962 C×.243), green pinned
 *   to #001b00 / #0b2a0b / #1d4a1d / #d8ffd8 with #39ff44 as the accent. The accent as text is
 *   the tint's light companion (palette.ts); green keeps #39ff44.
 */
import { type Gamut, type RGB, hexToRgb, mixOver, relativeOklch } from "./badges/color.ts";
import { TINT_COLORS, type Tint } from "./themeConfig.ts";
import { COMPANIONS } from "./palette.ts";

export interface Palette {
  bg: RGB;
  panel: RGB;
  border: RGB;
  /** Body text. */
  text: RGB;
  /** The accent used as text, focus rings, icons. */
  accent: RGB;
  /** A chip: its fill and the ink on it. */
  fill: RGB;
  fillInk: RGB;
}

const WHITE: RGB = [1, 1, 1];
const hex = hexToRgb;

/** Tints whose vivid ink is white (the tint is too dark for a dark ink). */
export const WHITE_INK: readonly Tint[] = ["tint-blue", "tint-red", "tint-purple"];
/** Tints whose vivid panel is the tint lightened (a mix with white), not darkened. */
export const LIGHT_PANEL: readonly Tint[] = ["tint-orange", "tint-charcoal"];
/** Light-theme canvas and panel for the neutral tint: gray, not near-white (the others derive theirs from the tint). */
export const LIGHT_NEUTRAL: Partial<Record<Tint, { bg: string; panel: string }>> = { "tint-charcoal": { bg: "#ececec", panel: "#e0e0e0" } };
export const LIGHT_PANEL_WHITE = 0.2;

export function vividPalette(tint: Tint, gamut: Gamut = "css"): Palette {
  const t = TINT_COLORS[tint];
  const bg = hex(t);
  const green = tint === "tint-green";
  const ink = green ? hex("#001b00") : WHITE_INK.includes(tint) ? WHITE : relativeOklch(t, 0.192, (c) => c * 0.243, gamut);
  const panel = green ? hex("#2ee03a")
    : LIGHT_PANEL.includes(tint) ? mixOver(WHITE, LIGHT_PANEL_WHITE, bg)
    : relativeOklch(t, (l) => l - 0.082, (c) => c * 0.91, gamut);
  // Chips are ink-filled with the tint written on them: no tint-on-tint text.
  return { bg, panel, border: ink, text: ink, accent: ink, fill: ink, fillInk: bg };
}

export function deepPalette(tint: Tint, gamut: Gamut = "css"): Palette {
  const t = TINT_COLORS[tint];
  if (tint === "tint-green") {
    const bg = hex("#001b00");
    return { bg, panel: hex("#0b2a0b"), border: hex("#1d4a1d"), text: hex("#d8ffd8"), accent: hex("#39ff44"), fill: hex(t), fillInk: bg };
  }
  const bg = relativeOklch(t, 0.192, (c) => c * 0.243, gamut);
  return {
    bg,
    panel: relativeOklch(t, 0.253, (c) => c * 0.243, gamut),
    border: relativeOklch(t, 0.366, (c) => c * 0.328, gamut),
    text: relativeOklch(t, 0.962, (c) => c * 0.243, gamut),
    accent: hex(COMPANIONS[tint].light),
    fill: hex(t),
    fillInk: WHITE_INK.includes(tint) ? WHITE : bg,
  };
}

const toHex = (rgb: RGB) => "#" + rgb.map((x) => Math.round(x * 255).toString(16).padStart(2, "0")).join("");
export const rgbHex = toHex;
