/**
 * The text switch: one on/off control beside the three circles, shown only in vivid. Off is vivid's own ink
 * (white on blue, crimson and purple; near-black on green, orange and charcoal); on flips it to the other one,
 * pure black or pure white, over the whole page. Saved in this browser like the theme (prefs.ts); nothing goes
 * to the relay. In light and dark the theme's ink is already near-black or near-white, so there is nothing to
 * flip and the switch is not shown.
 *
 * It is a style choice: the flipped ink is under the 4.5:1 body-text bar on every vivid tint (about 3:1 on the
 * canvas, less on panels), and the switch says so. Where the flip is unreadable (under FLIP_MIN on the canvas:
 * white on green) it is disabled with the reason. The CSS (dashboard.css, "TEXT SWITCH") applies it at the
 * token level: --theme-text, the button and accent-as-text tokens and --vv-ink, so every component follows.
 */
import { contrast, type Gamut, type RGB } from "./badges/color.ts";
import { themeColors } from "./badges/tokens.ts";
import type { Theme, Tint } from "./themeConfig.ts";

export const LS_INK = "emporia_ink";
export const INK_AA = 4.5;
/** The least the flipped ink may reach on the vivid canvas; below it the switch is disabled. */
export const FLIP_MIN = 2.5;

const BLACK: RGB = [0, 0, 0];
const WHITE: RGB = [1, 1, 1];

/** A saved value → on or off. The earlier Auto / White / Black choice maps to off / on / on. */
export function normalizeInk(v: string | null | undefined): boolean {
  return v === "on" || v === "white" || v === "black";
}

export interface PureInk {
  /** The ink the switch would use here. */
  color: "#000000" | "#ffffff";
  /** Whether the switch is shown at all (vivid only). */
  shown: boolean;
  /** Whether the switch can be on here. */
  ok: boolean;
  /** Why it cannot, in a sentence (empty when ok). */
  why: string;
  /** The flipped ink's contrast on the vivid canvas (worst of both gamuts). */
  worst: number;
}

const luminance = (c: RGB) => {
  const lin = (v: number) => (v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4);
  return 0.2126 * lin(c[0]) + 0.7152 * lin(c[1]) + 0.0722 * lin(c[2]);
};

export function pureInk(theme: Theme, tint: Tint): PureInk {
  if (theme !== "theme-vivid") return { color: "#000000", shown: false, ok: false, why: "", worst: 0 };
  let worst = Infinity;
  let flipWhite = false;
  for (const gamut of ["css", "clip"] as Gamut[]) {
    const t = themeColors("animatic", "vivid", tint, gamut);
    flipWhite = luminance(t.ink) < 0.5;                 // vivid's own ink is dark here: the flip is white
    const canvas = t.surfaces.find((x) => x.name === "canvas") ?? t.surfaces[0];
    worst = Math.min(worst, contrast(flipWhite ? WHITE : BLACK, canvas.rgb));
  }
  const color = flipWhite ? "#ffffff" : "#000000";
  const word = flipWhite ? "White" : "Black";
  if (worst < FLIP_MIN) {
    return { color, shown: true, ok: false, worst, why: `${word} text would be unreadable on this vivid tint (${worst.toFixed(1)}:1)` };
  }
  return { color, shown: true, ok: true, why: "", worst };
}
