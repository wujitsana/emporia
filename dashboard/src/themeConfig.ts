export const THEMES = ["theme-dark", "theme-light"] as const;
export type Theme = (typeof THEMES)[number];

export const TINTS = [
  "",
  "tint-green",
  "tint-blue",
  "tint-red",
  "tint-yellow",
  "tint-purple",
  "tint-orange",
  "tint-pink",
] as const;
export type Tint = (typeof TINTS)[number];

export const TINT_LABELS: Record<Tint, string> = {
  "": "Amber",
  "tint-green": "Green",
  "tint-blue": "Blue",
  "tint-red": "Red",
  "tint-yellow": "Yellow",
  "tint-purple": "Purple",
  "tint-orange": "Orange",
  "tint-pink": "Pink",
};

export const TINT_COLORS: Record<Tint, string> = {
  "": "#f0a832",
  "tint-green": "#39ff44",
  "tint-blue": "#0047ff",
  "tint-red": "#ff0000",
  "tint-yellow": "#e4f221",
  "tint-purple": "#8000ff",
  "tint-orange": "#ffac1c",
  "tint-pink": "#ff00ff",
};

export function toggleTheme(current: Theme): Theme {
  return current === "theme-dark" ? "theme-light" : "theme-dark";
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
 * Put the theme and tint on the app's root. When either *changes*, colours
 * cross-fade (dashboard.css § 28) for a moment; the first paint does not fade,
 * and `prefers-reduced-motion` turns the fade off in CSS. Back-to-back changes
 * (a run through the tints) keep the class on until the last one settles.
 * `fade: false` (the capture hotkeys, capture mode) switches instantly.
 */
export function applyTheme(theme: Theme, tint: Tint, opts: { fade?: boolean; capture?: boolean } = {}) {
  // On the Desktop page the body is the app's: the theme goes on our own root or nowhere.
  const b = themeRoot ?? (import.meta.env.VITE_EMPORIA_MODE === "desktop" ? null : document.body);
  if (!b) return;
  b.classList.toggle("e-capture", !!opts.capture);
  const had = THEMES.some((t) => b.classList.contains(t));
  const changed = had && (!b.classList.contains(theme) || (tint ? !b.classList.contains(tint) : TINTS.some((t) => t && b.classList.contains(t))));
  if (opts.fade === false) {
    clearTimeout(fadeTimers.get(b));
    b.classList.remove("e-theme-anim");
  } else if (changed) {
    b.classList.add("e-theme-anim");
    clearTimeout(fadeTimers.get(b));
    fadeTimers.set(b, setTimeout(() => b.classList.remove("e-theme-anim"), THEME_FADE_MS));
  }
  THEMES.forEach((t) => b.classList.remove(t));
  TINTS.forEach((t) => t && b.classList.remove(t));
  b.classList.add(theme);
  b.classList.add("font-use-CommitMono");
  if (tint) b.classList.add(tint);
}