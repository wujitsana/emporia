export const THEMES = ["theme-light", "theme-vivid", "theme-dark"] as const;
export type Theme = (typeof THEMES)[number];
/** The two the mode circle switches between; vivid is the middle circle's own toggle. */
export type BaseTheme = "theme-light" | "theme-dark";

/** Green is the default tint (the no-tint fallback) and the first in the cycle. */
export const TINTS = [
  "tint-green",
  "tint-blue",
  "tint-red",
  "tint-orange",
  "tint-purple",
  "tint-charcoal",
] as const;
export type Tint = (typeof TINTS)[number];
export const DEFAULT_TINT: Tint = "tint-green";

export const TINT_LABELS: Record<Tint, string> = {
  "tint-green": "Green",
  "tint-blue": "Blue",
  "tint-red": "Crimson",
  "tint-orange": "Orange",
  "tint-purple": "Purple",
  "tint-charcoal": "Charcoal",
};

export const TINT_COLORS: Record<Tint, string> = {
  "tint-green": "#39ff44",
  "tint-blue": "#0047ff",
  "tint-red": "#b00331",
  "tint-orange": "#d57c22",
  "tint-purple": "#7d3c98",
  "tint-charcoal": "#888888",
};

/** A saved or typed tint → a tint. The old default (`""`, "amber") and anything unknown load as green. */
export function normalizeTint(v: string | null | undefined): Tint {
  return (TINTS as readonly string[]).includes(v ?? "") ? (v as Tint) : DEFAULT_TINT;
}

/** A saved theme → a theme; unknown reads as light. */
export function normalizeTheme(v: string | null | undefined): Theme {
  return (THEMES as readonly string[]).includes(v ?? "") ? (v as Theme) : "theme-light";
}

/** A saved base (light | dark) → one; unknown falls back to the theme's own base, else light. */
export function normalizeBase(v: string | null | undefined, theme?: Theme): BaseTheme {
  if (v === "theme-light" || v === "theme-dark") return v;
  return theme === "theme-dark" ? "theme-dark" : "theme-light";
}

/** The mode circle: light ⇄ dark. From vivid it leaves vivid for the *other* of the two. */
export function toggleTheme(current: Theme, base: BaseTheme = "theme-light"): Theme {
  const from = current === "theme-vivid" ? base : current;
  return from === "theme-dark" ? "theme-light" : "theme-dark";
}

/** The vivid circle: on, or off back to the last of light / dark. */
export function toggleVivid(current: Theme, base: BaseTheme): Theme {
  return current === "theme-vivid" ? base : "theme-vivid";
}

export function nextTint(current: Tint): Tint {
  const i = TINTS.indexOf(current);
  const next = i < 0 ? 0 : (i + 1) % TINTS.length;
  return TINTS[next];
}

// P126: inside the Hermes dashboard the app lives in a shadow root, and on the
// Desktop page in a scoped container; theme classes go on that container —
// never on Hermes's <body>.
let themeRoot: HTMLElement | null = null;

export function setThemeRoot(el: HTMLElement | null) {
  themeRoot = el;
}

/** How long the cross-fade class stays on after a change; a little over the CSS duration. */
export const THEME_FADE_MS = 450;
const fadeTimers = new WeakMap<HTMLElement, ReturnType<typeof setTimeout>>();

/**
 * Put the theme and tint on the app's root. When either *changes*, colors
 * cross-fade (dashboard.css § 28) for a moment; the first paint does not fade,
 * and `prefers-reduced-motion` turns the fade off in CSS. Back-to-back changes
 * (a run through the tints) keep the class on until the last one settles.
 * `fade: false` (the capture hotkeys, capture mode) switches instantly.
 */
export function applyTheme(theme: Theme, tintIn: Tint | "", opts: { fade?: boolean; capture?: boolean } = {}) {
  const tint = normalizeTint(tintIn);
  // On the Desktop page the body is the app's: the theme goes on our own root or nowhere.
  const b = themeRoot ?? (import.meta.env.VITE_EMPORIA_MODE === "desktop" ? null : document.body);
  if (!b) return;
  b.classList.toggle("e-capture", !!opts.capture);
  const had = THEMES.some((t) => b.classList.contains(t));
  const changed = had && (!b.classList.contains(theme) || !b.classList.contains(tint));
  if (opts.fade === false) {
    clearTimeout(fadeTimers.get(b));
    b.classList.remove("e-theme-anim");
  } else if (changed) {
    b.classList.add("e-theme-anim");
    clearTimeout(fadeTimers.get(b));
    fadeTimers.set(b, setTimeout(() => b.classList.remove("e-theme-anim"), THEME_FADE_MS));
  }
  THEMES.forEach((t) => b.classList.remove(t));
  TINTS.forEach((t) => b.classList.remove(t));
  b.classList.remove("tint-amber");
  b.classList.add(theme);
  b.classList.add("font-use-CommitMono");
  b.classList.add("e-look");   // the one look (dashboard.css § 30)
  b.classList.add(tint);
}