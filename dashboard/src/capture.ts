/**
 * Capture mode, for filming the dashboard (the demo video's kaleidoscope shot).
 *
 *   ?capture=1        no transitions or animations at all, the relay's host:port
 *                     hidden, a phone layout scaled so its smallest text is ≥ 13
 *                     CSS px, and faster refreshes so changes appear on cue.
 *   ?tint=green       start on a tint: amber, green, blue, red, yellow, purple,
 *                     orange or pink. Not saved: the next plain load is unchanged.
 *   ?theme=light|dark start on a theme.
 *   t / T (hotkeys)   next tint / light ↔ dark, instantly (no fade), so a capture
 *                     script can switch on the beat. Never while typing.
 *
 * Like every control here, it changes only what is shown.
 */
import { TINTS, type Theme, type Tint } from "./themeConfig";

function params(): URLSearchParams {
  try {
    return new URLSearchParams(typeof window !== "undefined" ? window.location.search : "");
  } catch {
    return new URLSearchParams();
  }
}

export const CAPTURE = params().get("capture") === "1";

/** `?tint=green` → "tint-green"; `amber` (the default) → ""; anything else → null. */
export function tintFromParam(raw: string | null | undefined): Tint | null {
  const v = (raw ?? "").trim().toLowerCase().replace(/^tint-/, "");
  if (!v) return null;
  if (v === "amber" || v === "default") return "";
  const t = `tint-${v}` as Tint;
  return (TINTS as readonly string[]).includes(t) ? t : null;
}

export function themeFromParam(raw: string | null | undefined): Theme | null {
  const v = (raw ?? "").trim().toLowerCase().replace(/^theme-/, "");
  return v === "light" ? "theme-light" : v === "dark" ? "theme-dark" : null;
}

export const PARAM_TINT = tintFromParam(params().get("tint"));
export const PARAM_THEME = themeFromParam(params().get("theme"));

/** A poll interval, shortened in capture mode so a quest tick or a reveal shows up on cue. */
export function poll(ms: number): number {
  return CAPTURE ? Math.min(ms, 3_000) : ms;
}
