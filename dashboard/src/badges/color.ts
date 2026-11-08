/**
 * Just enough colour science to check a badge the way the style work checked text:
 * OKLCH ⇄ sRGB (CSS Color 4's relative colour syntax, `oklch(from <tint> L C H)`),
 * WCAG relative luminance and contrast. Pure; used by the tests and the gallery.
 *
 * A colour outside sRGB is brought back in two ways, and a check should pass both:
 * per-channel clipping (what some engines do) and the CSS Color 4 gamut mapping
 * (chroma reduced in OKLCH until the clipped colour is within a just-noticeable difference).
 */

export type RGB = readonly [number, number, number]; // sRGB, 0–1 (gamma-encoded)
export interface OKLCH { l: number; c: number; h: number }
export type Gamut = "clip" | "css";

export function hexToRgb(hex: string): RGB {
  const h = hex.replace(/^#/, "");
  const v = h.length === 3 ? h.split("").map((ch) => ch + ch).join("") : h.slice(0, 6);
  const n = parseInt(v, 16);
  return [((n >> 16) & 255) / 255, ((n >> 8) & 255) / 255, (n & 255) / 255];
}

const toLinear = (x: number) => (x <= 0.04045 ? x / 12.92 : ((x + 0.055) / 1.055) ** 2.4);
const toGamma = (x: number) => (x <= 0.0031308 ? 12.92 * x : 1.055 * x ** (1 / 2.4) - 0.055);

function linearToOklab([r, g, b]: RGB): [number, number, number] {
  const l = Math.cbrt(0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b);
  const m = Math.cbrt(0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b);
  const s = Math.cbrt(0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b);
  return [
    0.2104542553 * l + 0.793617785 * m - 0.0040720468 * s,
    1.9779984951 * l - 2.428592205 * m + 0.4505937099 * s,
    0.0259040371 * l + 0.7827717662 * m - 0.808675766 * s,
  ];
}

function oklabToLinear([L, a, b]: readonly [number, number, number]): RGB {
  const l = (L + 0.3963377774 * a + 0.2158037573 * b) ** 3;
  const m = (L - 0.1055613458 * a - 0.0638541728 * b) ** 3;
  const s = (L - 0.0894841775 * a - 1.291485548 * b) ** 3;
  return [
    4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s,
    -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s,
    -0.0041960863 * l - 0.7034186147 * m + 1.707614701 * s,
  ];
}

export function rgbToOklch(rgb: RGB): OKLCH {
  const [L, a, b] = linearToOklab(rgb.map(toLinear) as unknown as RGB);
  const c = Math.hypot(a, b);
  const h = c < 1e-6 ? 0 : ((Math.atan2(b, a) * 180) / Math.PI + 360) % 360;
  return { l: L, c, h };
}

function oklchToLab({ l, c, h }: OKLCH): [number, number, number] {
  const r = (h * Math.PI) / 180;
  return [l, c * Math.cos(r), c * Math.sin(r)];
}

/** Unclipped gamma-encoded sRGB (may fall outside 0–1). */
function oklchToRgbRaw(o: OKLCH): RGB {
  return oklabToLinear(oklchToLab(o)).map((x) => Math.sign(x) * toGamma(Math.abs(x))) as unknown as RGB;
}

const clip = (rgb: RGB): RGB => rgb.map((x) => Math.min(1, Math.max(0, x))) as unknown as RGB;
const inGamut = (rgb: RGB) => rgb.every((x) => x >= -1e-6 && x <= 1 + 1e-6);

function deltaEOK(a: RGB, b: RGB): number {
  const p = linearToOklab(a.map(toLinear) as unknown as RGB);
  const q = linearToOklab(b.map(toLinear) as unknown as RGB);
  return Math.hypot(p[0] - q[0], p[1] - q[1], p[2] - q[2]);
}

/** OKLCH → displayable sRGB. */
export function oklchToRgb(o: OKLCH, gamut: Gamut = "css"): RGB {
  const raw = oklchToRgbRaw(o);
  if (inGamut(raw)) return clip(raw);
  if (gamut === "clip") return clip(raw);
  if (o.l >= 1) return [1, 1, 1];
  if (o.l <= 0) return [0, 0, 0];
  // CSS Color 4 §13.2: binary search on chroma, accepting a clipped colour within the JND.
  const JND = 0.02;
  let lo = 0;
  let hi = o.c;
  let lastClipped = clip(oklchToRgbRaw({ ...o, c: 0 }));
  while (hi - lo > 0.0001) {
    const mid = (lo + hi) / 2;
    const cand = oklchToRgbRaw({ ...o, c: mid });
    if (inGamut(cand)) { lo = mid; lastClipped = clip(cand); continue; }
    const clipped = clip(cand);
    if (deltaEOK(clipped, cand) < JND) {
      lo = mid;
      lastClipped = clipped;
      if (JND - deltaEOK(clipped, cand) < 0.0001) break;
    } else {
      hi = mid;
    }
  }
  return lastClipped;
}

/** A number, or a function of the origin's channel: how CSS's `oklch(from …)` reads `l`, `c`. */
export type Channel = number | ((v: number) => number);

/** `oklch(from <hex> L C h)`: the origin's hue, L and C as given (numbers or functions of l, c). */
export function relativeOklch(hex: string, L: Channel, C: Channel, gamut: Gamut = "css"): RGB {
  const o = rgbToOklch(hexToRgb(hex));
  const l = typeof L === "function" ? L(o.l) : L;
  const c = typeof C === "function" ? C(o.c) : C;
  return oklchToRgb({ l, c: Math.max(0, c), h: o.h }, gamut);
}

/** `color-mix(in srgb, a p%, transparent)` laid over `under`: a straight blend. */
export function mixOver(a: RGB, p: number, under: RGB): RGB {
  return a.map((x, i) => x * p + under[i] * (1 - p)) as unknown as RGB;
}

export function luminance(rgb: RGB): number {
  const [r, g, b] = rgb.map(toLinear);
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

/** The WCAG contrast ratio, 1–21. */
export function contrast(a: RGB, b: RGB): number {
  const la = luminance(a);
  const lb = luminance(b);
  return (Math.max(la, lb) + 0.05) / (Math.min(la, lb) + 0.05);
}

/** WCAG 2.x non-text contrast (1.4.11): graphics need 3:1 against what they sit on. */
export const NON_TEXT_MIN = 3;
