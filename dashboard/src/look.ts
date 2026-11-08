/**
 * The look's moving parts, shared by the section heading and the header's brand:
 * whether they may move, and the scramble they decode from (the demo's title card,
 * `E#P0R!A` → `EMPORIA`).
 *
 * There is one style (dashboard.css § 30). Light/dark and the eight tints stay the
 * viewer's choice (`t`, `T`, `?tint=`, `?theme=`); nothing else about the look is.
 */
import { CAPTURE } from "./capture";

/** Whether arrivals may move: not when filming, not when the viewer asked for less motion. */
export function motionAllowed(): boolean {
  if (CAPTURE) return false;
  try {
    return !(typeof window !== "undefined" && window.matchMedia?.("(prefers-reduced-motion: reduce)").matches);
  } catch {
    return true;
  }
}

const GLYPHS = "#!%&*+=?<>/\\0123456789ABCDEFGHJKLMNPQRSTUVWXYZ";

/** `text` with its first `resolved` letters in place and the rest scrambled; spaces stay spaces. */
export function scrambleFrame(text: string, resolved: number, rnd: () => number = Math.random): string {
  let out = "";
  for (let i = 0; i < text.length; i++) {
    const ch = text[i];
    out += i < resolved || ch === " " ? ch : GLYPHS[Math.floor(rnd() * GLYPHS.length)];
  }
  return out;
}
