/**
 * The text switch: one on/off control beside the three circles. Off is the theme's own ink (tinted);
 * on is pure black or pure white text over the whole page, whichever the theme's surfaces take.
 * Saved in this browser like the theme (prefs.ts); nothing goes to the relay.
 *
 * Contrast guard: pure ink is used only where it reaches 4.5:1 on the canvas AND the panel (both
 * gamuts); elsewhere the switch is disabled and says
 * why, instead of producing text nobody can read. The CSS (dashboard.css, "TEXT SWITCH") applies it at
 * the token level: --theme-text, the button and accent-as-text tokens and, in vivid, --vv-ink, so every
 * component that reads a token follows.
 */
import { contrast, type Gamut, type RGB } from "./badges/color.ts";
import { themeColors, type ThemeName } from "./badges/tokens.ts";
import type { Theme, Tint } from "./themeConfig.ts";

export const LS_INK = "emporia_ink";
export const INK_AA = 4.5;

const BLACK: RGB = [0, 0, 0];
const WHITE: RGB = [1, 1, 1];

/** A saved value → on or off. The earlier Auto / White / Black choice maps to off / on / on. */
export function normalizeInk(v: string | null | undefined): boolean {
  return v === "on" || v === "white" || v === "black";
}

export interface PureInk {
  /** The ink the switch would use here. */
  color: "#000000" | "#ffffff";
  /** Whether the switch can be on here. */
  ok: boolean;
  /** Why it cannot, in a sentence (empty when ok). */
  why: string;
  /** The worst contrast of that ink on the canvas and the panel. */
  worst: number;
}

export function pureInk(theme: Theme, tint: Tint): PureInk {
  const name = theme.replace("theme-", "") as ThemeName;
  const score = (ink: RGB) => {
    let worst = Infinity;
    for (const gamut of ["css", "clip"] as Gamut[]) {
      const t = themeColors("animatic", name, tint, gamut);
      for (const s of t.surfaces.filter((x) => x.name !== "wash")) worst = Math.min(worst, contrast(ink, s.rgb));
    }
    return { worst };
  };
  const b = score(BLACK);
  const w = score(WHITE);
  const useWhite = w.worst > b.worst;
  const pick = useWhite ? w : b;
  const color = useWhite ? "#ffffff" : "#000000";
  if (pick.worst < INK_AA) {
    return { color, ok: false, worst: pick.worst, why: `Pure black or white text would be low contrast here (${pick.worst.toFixed(1)}:1, under ${INK_AA}:1)` };
  }
  return { color, ok: true, why: "", worst: pick.worst };
}
