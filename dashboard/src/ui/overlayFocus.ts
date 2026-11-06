/**
 * Focus for the app's two overlays, the phone menu and the agent chat.
 *
 * Opening one moves the focus into it, so a keyboard or screen-reader user is
 * where they just asked to be, and Escape reaches the overlay that is on top
 * (with the menu opened over full-page chat, Escape used to close the chat
 * underneath, because the focus had stayed on the chat's ☰). Closing it puts
 * the focus back where it was, unless that is now inert or gone.
 *
 * `NARROW` is the header-nav breakpoint: below it the menu and the chat cover
 * the frame (dashboard.css § 23, ChatDrawer.module.css), and what they cover is
 * made inert by App.
 */
import { useEffect, useRef, useSyncExternalStore, type RefObject } from "react";
import { focusedWithin } from "../keyTarget";

export const NARROW = "(max-width: 900px)";

function narrowList(): MediaQueryList | null {
  try {
    return typeof window !== "undefined" && window.matchMedia ? window.matchMedia(NARROW) : null;
  } catch {
    return null;
  }
}

export function isNarrow(): boolean {
  return narrowList()?.matches ?? false;
}

function subscribeNarrow(fn: () => void): () => void {
  const mq = narrowList();
  if (!mq) return () => {};
  mq.addEventListener?.("change", fn);
  return () => mq.removeEventListener?.("change", fn);
}

/** Whether the window is below the header-nav width, following resizes. */
export function useNarrow(): boolean {
  return useSyncExternalStore(subscribeNarrow, isNarrow, () => false);
}

const FOCUSABLE = 'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

/** The first focusable element in `root` that is drawn (else the first at all: jsdom draws nothing). */
export function firstFocusable(root: HTMLElement): HTMLElement | null {
  const all = [...root.querySelectorAll<HTMLElement>(FOCUSABLE)];
  return all.find((el) => el.getClientRects().length > 0) ?? all[0] ?? null;
}

/**
 * While `open`, the focus is inside `container` (on `initial()`, else its first
 * focusable element); when it closes, back on what had it before.
 */
export function useOverlayFocus(
  open: boolean,
  container: RefObject<HTMLElement | null>,
  initial?: () => HTMLElement | null,
): void {
  const pick = useRef(initial);
  useEffect(() => {
    pick.current = initial;
  });
  useEffect(() => {
    const el = container.current;
    if (!open || !el) return;
    const before = focusedWithin(el);
    const target = pick.current?.() ?? firstFocusable(el);
    target?.focus();
    return () => {
      if (before instanceof HTMLElement && before.isConnected && !before.closest("[inert]")) before.focus();
    };
  }, [open, container]);
}
