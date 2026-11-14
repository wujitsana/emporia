/**
 * The viewer's own look switches: the font (Pixel, Serif, Type) and the text ink (Auto, White,
 * Black). They live in this browser like the theme and the tint (prefs.ts); nothing goes to the relay.
 *
 * Ink rule: the title always follows White / Black. Body text follows it only where the ink reaches
 * 4.5:1 on that theme and tint's canvas and panel; elsewhere body text stays on Auto and the control
 * shows a "low contrast" mark. `bodyInkApplies` is that rule; themeContrast.test.ts checks it.
 */
import { contrast, type Gamut, type RGB } from "./badges/color.ts";
import { themeColors, type ThemeName } from "./badges/tokens.ts";
import type { Theme, Tint } from "./themeConfig.ts";

export const FONTS = ["pixel", "serif", "type"] as const;
export type Font = (typeof FONTS)[number];
export const DEFAULT_FONT: Font = "pixel";
export const FONT_LABELS: Record<Font, string> = { pixel: "Pixel", serif: "Serif", type: "Type" };

export const INKS = ["auto", "white", "black"] as const;
export type Ink = (typeof INKS)[number];
export const DEFAULT_INK: Ink = "auto";
export const INK_LABELS: Record<Ink, string> = { auto: "Auto", white: "White", black: "Black" };
export const INK_COLORS: Record<Exclude<Ink, "auto">, string> = { white: "#ffffff", black: "#000000" };

export const LS_FONT = "emporia_font";
export const LS_INK = "emporia_ink";

export const BODY_AA = 4.5;

export function normalizeFont(v: string | null | undefined): Font {
  return (FONTS as readonly string[]).includes(v ?? "") ? (v as Font) : DEFAULT_FONT;
}
export function normalizeInk(v: string | null | undefined): Ink {
  return (INKS as readonly string[]).includes(v ?? "") ? (v as Ink) : DEFAULT_INK;
}
export function nextInk(c: Ink): Ink {
  return INKS[(INKS.indexOf(c) + 1) % INKS.length];
}

const RGBS: Record<Exclude<Ink, "auto">, RGB> = { white: [1, 1, 1], black: [0, 0, 0] };

/** The contrast the ink makes on the theme's worst surface (canvas or panel), in the worse of both gamuts. */
export function inkContrast(theme: Theme, tint: Tint, ink: Exclude<Ink, "auto">): number {
  const name = theme.replace("theme-", "") as ThemeName;
  let worst = Infinity;
  for (const gamut of ["css", "clip"] as Gamut[]) {
    const t = themeColors("animatic", name, tint, gamut);
    for (const s of t.surfaces.filter((x) => x.name !== "wash")) worst = Math.min(worst, contrast(RGBS[ink], s.rgb));
  }
  return worst;
}

/** Whether body text may use this ink on this theme and tint (Auto always may; the title always follows the choice). */
export function bodyInkApplies(theme: Theme, tint: Tint, ink: Ink): boolean {
  return ink === "auto" || inkContrast(theme, tint, ink) >= BODY_AA;
}
