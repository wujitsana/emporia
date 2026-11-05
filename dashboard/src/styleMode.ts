/**
 * The style mode: a third look axis, beside light/dark and the accent tint.
 *
 *   retromod   the dashboard as it is (the default)
 *   animatic   the demo animatic's information cards: pixel headings that
 *              unscramble, mono text typed with a block cursor, corner brackets,
 *              status lines above and below, tint fills with dark ink
 *              (styleAnimatic.css; docs/ui-style-experimental.md)
 *
 * A per-viewer preference, like the theme: kept in this browser's localStorage
 * (every access wrapped, so a blocked or broken storage falls back to the
 * default), never sent to a relay. `?style=animatic` sets it for one load
 * without saving it; `s` (outside text fields) switches it instantly.
 *
 * The animatic style is one class on the app's root (the same element that
 * carries the theme and tint), and every rule for it is scoped under that
 * class, so the default style is untouched when it is off.
 */
import { useEffect, useSyncExternalStore } from "react";
import { CAPTURE } from "./capture";
import { isTyping } from "./keyTarget";
import { themeRootEl } from "./themeConfig";

export const STYLES = ["retromod", "animatic"] as const;
export type Style = (typeof STYLES)[number];
export const DEFAULT_STYLE: Style = "retromod";

export const STYLE_LABELS: Record<Style, string> = {
  retromod: "Retromod",
  animatic: "Animatic",
};

export const STYLE_ABOUT: Record<Style, string> = {
  retromod: "The dashboard's own look: monospace, flat panels, the accent as a line.",
  animatic: "Experimental, from the demo animatic's cards: pixel headings, typed lines, corner brackets, status lines, tint fills.",
};

/** The class each style puts on the app's root; the default puts none. */
export const STYLE_CLASS: Record<Style, string> = {
  retromod: "",
  animatic: "style-animatic",
};

export const LS_STYLE = "emporia_style";

/** `?style=animatic` → "animatic"; `default` → the default; anything else → null. */
export function styleFromParam(raw: string | null | undefined): Style | null {
  const v = (raw ?? "").trim().toLowerCase().replace(/^style-/, "");
  if (!v) return null;
  if (v === "default") return DEFAULT_STYLE;
  return (STYLES as readonly string[]).includes(v) ? (v as Style) : null;
}

type Store = Pick<Storage, "getItem" | "setItem">;

function localStore(): Store | null {
  try {
    return typeof localStorage !== "undefined" ? localStorage : null;
  } catch {
    return null;
  }
}

function search(): string {
  try {
    return typeof window !== "undefined" ? window.location.search : "";
  } catch {
    return "";
  }
}

/**
 * The style for this load: `?style=` wins (not saved); capture mode, which films
 * the default unless told otherwise, ignores what was saved; then the saved one.
 */
export function readStyle(opts: { storage?: Store | null; query?: string; capture?: boolean } = {}): Style {
  let param: Style | null = null;
  try {
    param = styleFromParam(new URLSearchParams(opts.query ?? search()).get("style"));
  } catch {
    param = null;
  }
  if (param) return param;
  if (opts.capture ?? CAPTURE) return DEFAULT_STYLE;
  const store = opts.storage === undefined ? localStore() : opts.storage;
  try {
    const v = store?.getItem(LS_STYLE);
    return (STYLES as readonly string[]).includes(v ?? "") ? (v as Style) : DEFAULT_STYLE;
  } catch {
    return DEFAULT_STYLE;
  }
}

/** Keep the choice in this browser. Storage that is full, blocked or gone is not an error. */
export function saveStyle(style: Style, storage?: Store | null): boolean {
  const store = storage === undefined ? localStore() : storage;
  try {
    store?.setItem(LS_STYLE, style);
    return !!store;
  } catch {
    return false;
  }
}

/** Put the style's class on the root and take every other style's off. */
export function applyStyle(style: Style, root: HTMLElement = themeRootEl()) {
  for (const s of STYLES) if (STYLE_CLASS[s]) root.classList.remove(STYLE_CLASS[s]);
  if (STYLE_CLASS[style]) root.classList.add(STYLE_CLASS[style]);
}

export function nextStyle(current: Style): Style {
  return STYLES[(STYLES.indexOf(current) + 1) % STYLES.length];
}

// One shared value, so Settings, the hotkey and the chrome agree without props.
let current: Style | null = null;
const listeners = new Set<() => void>();

export function getStyle(): Style {
  if (current === null) current = readStyle();
  return current;
}

/** Change the style; saved unless this load is a capture or a `?style=` one-off. */
export function setStyle(style: Style, opts: { save?: boolean } = {}) {
  current = style;
  const oneOff = CAPTURE || styleFromParam(new URLSearchParams(search()).get("style")) !== null;
  if (opts.save ?? !oneOff) saveStyle(style);
  listeners.forEach((l) => l());
}

function subscribe(l: () => void) {
  listeners.add(l);
  return () => listeners.delete(l);
}

/** The current style and its setter, for any component. */
export function useStyleMode(): [Style, (s: Style) => void] {
  const style = useSyncExternalStore(subscribe, getStyle, getStyle);
  return [style, setStyle];
}

/** Whether the animatic's entrances may move: not when filming, not when the viewer asked for less motion. */
export function motionAllowed(): boolean {
  if (CAPTURE) return false;
  try {
    return !(typeof window !== "undefined" && window.matchMedia?.("(prefers-reduced-motion: reduce)").matches);
  } catch {
    return true;
  }
}

/**
 * Mount once, in App: keeps the root's class in step with the style, and adds
 * the `s` hotkey (never while typing; inside the Hermes tab the app asks its
 * own root what has focus, as the other hotkeys do).
 */
export function useStyleRoot(appNode: { current: Node | null }) {
  const [style] = useStyleMode();
  useEffect(() => {
    applyStyle(style);
  }, [style]);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "s" || e.metaKey || e.ctrlKey || e.altKey) return;
      if (isTyping(e, appNode.current)) return;
      setStyle(nextStyle(getStyle()));
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [appNode]);
  return style;
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

/** Tests only: forget the shared value so the next read starts fresh. */
export function _resetStyleForTests() {
  current = null;
  listeners.clear();
}
