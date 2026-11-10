import type { KeyboardEvent } from "react";

/**
 * Arrow keys, Home and End move between the buttons of a row of tabs: the row is one stop on the way
 * through the page, not a dozen Tab presses.
 */
export function arrowKeys(e: KeyboardEvent<HTMLElement>): void {
  const keys = ["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "Home", "End"];
  if (!keys.includes(e.key)) return;
  const tabs = [...e.currentTarget.querySelectorAll<HTMLElement>("button:not([disabled])")];
  const root = e.currentTarget.getRootNode() as Document | ShadowRoot;
  const at = tabs.indexOf(root.activeElement as HTMLElement);
  if (at < 0 || tabs.length < 2) return;
  e.preventDefault();
  const next = e.key === "Home" ? 0 : e.key === "End" ? tabs.length - 1
    : (at + (e.key === "ArrowLeft" || e.key === "ArrowUp" ? -1 : 1) + tabs.length) % tabs.length;
  tabs[next].focus();
}
