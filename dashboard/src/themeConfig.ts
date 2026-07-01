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

export function applyTheme(theme: Theme, tint: Tint) {
  const b = document.body;
  THEMES.forEach((t) => b.classList.remove(t));
  TINTS.forEach((t) => t && b.classList.remove(t));
  b.classList.add(theme);
  b.classList.add("font-use-CommitMono");
  if (tint) b.classList.add(tint);
}